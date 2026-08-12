import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from benchmarks import production_gate
from benchmarks.production_gate import percentile, sustained_load
from benchmarks.report import markdown
from benchmarks.run import render, rotated_provider_indexes, run_benchmark


class BenchmarkReportTests(unittest.TestCase):
    def test_report_preserves_success_and_latency_evidence(self):
        report = {
            "generated_at": "2026-08-11T00:00:00+00:00",
            "environment": {"python": "3.11", "platform": "test", "machine": "x86_64"},
            "configuration": {"runs": 2, "warmups": 1, "scenarios": []},
            "providers": [
                {
                    "type": "viper",
                    "cases": [
                        {
                            "name": "fixture",
                            "successes": 2,
                            "failures": [],
                            "latency_ms": {"median": 12.5, "p95": 14.0},
                            "artifacts": [{"bytes": 100}, {"bytes": 120}],
                        }
                    ],
                }
            ],
        }
        document = markdown(report, "Evidence")
        self.assertIn("| fixture | viper | 2/2 | 12.50 ms | 14.00 ms | 110 |", document)
        self.assertIn("Provider order was rotated for every attempt from the same benchmark process and host", document)

    def test_percentile_is_bounded_and_deterministic(self):
        self.assertEqual(percentile([40, 10, 20, 30], 0.95), 40)
        self.assertEqual(percentile([], 0.95), 0.0)

    def test_provider_order_rotates_deterministically_between_attempts(self):
        self.assertEqual(rotated_provider_indexes(2, 0), [0, 1])
        self.assertEqual(rotated_provider_indexes(2, 1), [1, 0])
        self.assertEqual(rotated_provider_indexes(3, 4), [1, 2, 0])
        self.assertEqual(rotated_provider_indexes(0, 2), [])


class BenchmarkInterleaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_benchmark_interleaves_provider_calls_between_attempts(self):
        calls = []
        output = io.BytesIO()
        Image.new("RGB", (8, 8)).save(output, "PNG")

        async def fake_render(_client, provider, _endpoint, _scenario):
            calls.append(provider)
            return output.getvalue()

        providers = [("viper", "http://viper"), ("browserless", "http://browserless")]
        scenarios = [{"name": "fixture", "request": {}}]
        async with httpx.AsyncClient() as client:
            with patch("benchmarks.run.render", side_effect=fake_render):
                results = await run_benchmark(client, providers, scenarios, 2, 0)

        self.assertEqual(calls, ["viper", "browserless", "browserless", "viper"])
        self.assertEqual([item["cases"][0]["successes"] for item in results], [2, 2])


class SustainedLoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_completes_at_least_the_requested_minimum(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            output = io.BytesIO()
            Image.new("RGB", (8, 8)).save(output, "PNG")
            return httpx.Response(200, content=output.getvalue())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("benchmarks.production_gate.httpx.AsyncClient") as client_type:
            client_type.return_value.__aenter__.return_value = client
            result = await sustained_load(
                "http://local", requests=3, concurrency=2, timeout=1
            )
        await client.aclose()

        self.assertGreaterEqual(calls, 3)
        self.assertEqual(result["requests"], calls)
        self.assertEqual(result["success_rate"], 1)

    async def test_memory_is_sampled_during_render_and_uses_cgroup_peak(self):
        state = {"in_flight": False, "observed": False}
        output = io.BytesIO()
        Image.new("RGB", (8, 8)).save(output, "PNG")

        class SlowTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                state["in_flight"] = True
                await production_gate.asyncio.sleep(0.04)
                state["in_flight"] = False
                return httpx.Response(200, content=output.getvalue())

        def cgroup_number(name):
            if name == "memory.current":
                state["observed"] = state["observed"] or state["in_flight"]
                return 200 if state["in_flight"] else 100
            if name == "memory.peak":
                return 250
            if name == "memory.max":
                return 1024
            return None

        client = httpx.AsyncClient(transport=SlowTransport())
        with patch("benchmarks.production_gate.httpx.AsyncClient") as client_type, patch(
            "benchmarks.production_gate._cgroup_number", side_effect=cgroup_number
        ):
            client_type.return_value.__aenter__.return_value = client
            result = await sustained_load(
                "http://local", requests=1, concurrency=1, timeout=1
            )
        await client.aclose()

        self.assertTrue(state["observed"])
        self.assertEqual(result["memory"]["peak_cgroup_bytes"], 250)


class BrowserlessAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_browserless_uses_same_viewport_and_full_page_contract(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["json"] = json.loads(request.content)
            return httpx.Response(200, content=b"image")

        scenario = {
            "url": "https://example.com",
            "output": "png",
            "viewport": {"width": 1280, "height": 720, "device_scale_factor": 2},
            "full_page": True,
            "wait_for": {
                "event": "domcontentloaded",
                "delay_ms": 1000,
                "selector": "main",
                "timeout_ms": 12000,
            },
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.dict("os.environ", {"BROWSERLESS_TOKEN": "local-token"}):
                body = await render(client, "browserless", "http://127.0.0.1:3000", scenario)

        self.assertEqual(body, b"image")
        self.assertEqual(
            captured["url"],
            "http://127.0.0.1:3000/chromium/screenshot?token=local-token",
        )
        self.assertEqual(
            captured["json"]["viewport"],
            {"width": 1280, "height": 720, "deviceScaleFactor": 2},
        )
        self.assertTrue(captured["json"]["options"]["fullPage"])
        self.assertEqual(
            captured["json"]["gotoOptions"],
            {"waitUntil": "domcontentloaded", "timeout": 12000},
        )
        self.assertEqual(captured["json"]["waitForTimeout"], 1000)
        self.assertEqual(
            captured["json"]["waitForSelector"],
            {"selector": "main", "timeout": 12000, "visible": True},
        )

    async def test_non_viper_provider_rejects_unequal_lazy_loading(self):
        scenario = {
            "url": "https://example.com",
            "viewport": {"width": 1280, "height": 720},
            "lazy_load": "adaptive",
        }
        async with httpx.AsyncClient() as client:
            with self.assertRaisesRegex(ValueError, "cannot preserve lazy_load=adaptive"):
                await render(
                    client, "browserless", "http://127.0.0.1:3000", scenario
                )

    def test_published_comparison_scenarios_disable_lazy_loading(self):
        scenarios = json.loads(
            (Path(__file__).parents[1] / "benchmarks" / "scenarios-real-sites.json").read_text(
                "utf-8"
            )
        )
        self.assertTrue(
            all(case["request"]["lazy_load"] == "none" for case in scenarios)
        )


class ManagedProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    scenario = {
        "url": "https://example.com",
        "output": "png",
        "viewport": {"width": 1280, "height": 720, "device_scale_factor": 2},
        "full_page": False,
        "wait_for": {
            "event": "domcontentloaded",
            "delay_ms": 1000,
            "selector": "main",
            "timeout_ms": 12000,
        },
    }

    async def test_screenshotone_preserves_wait_contract(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["json"] = json.loads(request.content)
            return httpx.Response(200, content=b"image")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.dict("os.environ", {"SCREENSHOTONE_ACCESS_KEY": "key"}):
                await render(client, "screenshotone", "https://api.example/take", self.scenario)

        self.assertEqual(captured["json"]["wait_until"], "domcontentloaded")
        self.assertEqual(captured["json"]["delay"], 1)
        self.assertEqual(captured["json"]["timeout"], 12)
        self.assertEqual(captured["json"]["wait_for_selector"], "main")
        self.assertTrue(captured["json"]["error_on_selector_not_found"])
        self.assertEqual(captured["json"]["device_scale_factor"], 2)

    async def test_urlbox_preserves_wait_contract(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                captured["json"] = json.loads(request.content)
                return httpx.Response(
                    200, json={"renderUrl": "https://renders.example/image.png"}
                )
            return httpx.Response(200, content=b"image")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.dict("os.environ", {"URLBOX_SECRET": "secret"}):
                await render(client, "urlbox", "https://api.example/render", self.scenario)

        self.assertEqual(captured["json"]["wait_until"], "domloaded")
        self.assertEqual(captured["json"]["delay"], 1000)
        self.assertEqual(captured["json"]["timeout"], 12000)
        self.assertEqual(captured["json"]["wait_for"], "main")
        self.assertEqual(captured["json"]["wait_for_state"], "visible")
        self.assertEqual(captured["json"]["wait_timeout"], 12000)
        self.assertTrue(captured["json"]["fail_if_selector_missing"])
        self.assertTrue(captured["json"]["retina"])

    async def test_urlbox_rejects_unrepresentable_device_scale(self):
        scenario = {
            **self.scenario,
            "viewport": {**self.scenario["viewport"], "device_scale_factor": 1.5},
        }
        async with httpx.AsyncClient() as client:
            with patch.dict("os.environ", {"URLBOX_SECRET": "secret"}):
                with self.assertRaisesRegex(ValueError, "supports device_scale_factor"):
                    await render(
                        client, "urlbox", "https://api.example/render", scenario
                    )


class RecoveryDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_keeps_log_and_writes_machine_readable_error(self):
        async def fail_recovery(_port: int, data_dir: Path):
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "operational-server.log").write_text("startup failed", "utf-8")
            raise RuntimeError("recovery failed")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "operational-results" / "restart-recovery.json"
            argv = ["production_gate", "restart-recovery", "--output", str(output)]
            with patch.object(production_gate, "restart_recovery", new=fail_recovery), patch(
                "sys.argv", argv
            ):
                exit_code = await production_gate.main()

            data_dir = output.parent / "restart-recovery-data"
            report = json.loads(output.read_text("utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertEqual(report["error"]["type"], "RuntimeError")
            self.assertEqual(report["error"]["message"], "recovery failed")
            self.assertEqual(
                (data_dir / "operational-server.log").read_text("utf-8"),
                "startup failed",
            )


if __name__ == "__main__":
    unittest.main()
