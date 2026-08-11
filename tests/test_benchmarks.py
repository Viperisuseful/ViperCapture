import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from benchmarks.production_gate import percentile, sustained_load
from benchmarks.report import markdown
from benchmarks.run import render


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
        self.assertIn("| fixture | viper | 2/2 | 12.50 ms | 14.00 ms | 120 |", document)
        self.assertIn("Providers were invoked sequentially from the same benchmark process and host", document)

    def test_percentile_is_bounded_and_deterministic(self):
        self.assertEqual(percentile([40, 10, 20, 30], 0.95), 40)
        self.assertEqual(percentile([], 0.95), 0.0)


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
            "viewport": {"width": 1280, "height": 720},
            "full_page": True,
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.dict("os.environ", {"BROWSERLESS_TOKEN": "local-token"}):
                body = await render(client, "browserless", "http://127.0.0.1:3000", scenario)

        self.assertEqual(body, b"image")
        self.assertEqual(
            captured["url"],
            "http://127.0.0.1:3000/chromium/screenshot?token=local-token",
        )
        self.assertEqual(captured["json"]["viewport"], {"width": 1280, "height": 720})
        self.assertTrue(captured["json"]["options"]["fullPage"])


if __name__ == "__main__":
    unittest.main()
