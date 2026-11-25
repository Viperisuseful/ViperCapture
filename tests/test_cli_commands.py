"""Unit and HTTP integration tests for ViperCapture CLI subcommands and config."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import launch
from vipercapture import cli_commands
from vipercapture.cli_config import _fallback_toml_loads, load_cli_config, read_version


def _make_diff_zip(passed: bool = True, ratio: float = 0.0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "report.json",
            json.dumps({
                "changed_pixels": 0 if passed else 12,
                "total_pixels": 100,
                "difference_ratio": ratio,
                "passed": passed,
                "bounding_box": None if passed else [0, 0, 4, 3],
            }),
        )
        zf.writestr("diff.png", b"\x89PNG\r\n\x1a\ndiff")
    return buf.getvalue()


class _ApiMockHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, doc: object, headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(doc).encode("utf-8")
        self._send_bytes(status, raw, "application/json", headers=headers)

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method: str) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({  # type: ignore[attr-defined]
            "method": method,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body,
        })
        return body

    def do_GET(self) -> None:
        self._record("GET")
        parsed = urlsplit(self.path)
        path = parsed.path
        state = self.server.state  # type: ignore[attr-defined]

        if path == "/health":
            self._send_json(200, {"ready": True})
            return
        if path == "/ready":
            ready = state.get("ready", True)
            self._send_json(200 if ready else 503, {"ready": ready, "role": "all", "async_jobs": True})
            return
        if path == "/app-config":
            doc = {
                "control_plane": True,
                "presets": True,
                "browser_engines": ["chromium", "firefox", "webkit"],
                "output_formats": ["png", "jpeg", "pdf"],
                "async_jobs": {"enabled": True, "workers": 4, "queue_limit": 200},
                "gpu": {"mode": state["gpu_mode"], "hardware_active": state["gpu_mode"] == "auto", "mutable": True},
            }
            if state.get("app_config_version") is not None:
                doc["version"] = state["app_config_version"]
            self._send_json(200, doc)
            return
        if path == "/openapi.json":
            oa_status = state.get("openapi_status", 200)
            if state.get("openapi_version") is not None:
                self._send_json(oa_status, {"openapi": "3.1.0", "info": {"title": "ViperCapture", "version": state["openapi_version"]}})
                return
            self._send_json(404, {"error": {"code": "not_found", "message": "Not found"}})
            return
        if path == "/local/presets":
            self._send_json(200, {"presets": state["presets"]})
            return
        if path == "/v1/baselines":
            self._send_json(200, list(state["baselines"].values()))
            return
        if path.startswith("/v1/jobs/") and path.endswith("/result"):
            job_id = path.split("/")[3]
            safe_id = job_id if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id) else "artifact"
            self._send_bytes(
                200,
                f"ARTIFACT-{safe_id}".encode("utf-8"),
                "image/png",
                headers={"Content-Disposition": 'attachment; filename="artifact.png"'},
            )
            return
        if path.startswith("/v1/jobs/"):
            job_id = path.split("/")[3]
            polls = state["job_polls"].get(job_id, 0) + 1
            state["job_polls"][job_id] = polls
            status = state.get("job_status_override") or ("succeeded" if polls >= 1 else "running")
            self._send_json(200, {
                "id": job_id,
                "status": status,
                "status_url": f"/v1/jobs/{job_id}",
                "result_url": f"/v1/jobs/{job_id}/result" if status == "succeeded" else None,
            })
            return
        if path == "/v1/schedules":
            self._send_json(200, {
                "count": len(state["schedules"]),
                "schedules": list(state["schedules"].values()),
                "next_cursor": None,
            })
            return
        if path.startswith("/v1/schedules/"):
            sid = path.split("/")[3]
            if sid not in state["schedules"]:
                self._send_json(404, {"error": {"code": "schedule_not_found", "message": "The schedule was not found."}})
                return
            self._send_json(200, state["schedules"][sid])
            return
        if path == "/v1/certification/public-key":
            self._send_json(200, {"algorithm": "Ed25519", "public_key": state["cert_public_key"]})
            return
        if path == "/v1/admin/projects":
            self._send_json(200, state["projects"])
            return
        if path == "/v1/admin/audit":
            self._send_json(200, [
                {"id": 1, "project_id": "proj1", "actor": "admin", "action": "project.created", "resource": "proj1", "created_at": 1700000000}
            ])
            return
        if path == "/v1/admin/status":
            self._send_json(200, {"role": "all", "browser_connected": True, "async_jobs": True, "control_plane": True})
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "Not found"}})

    def do_POST(self) -> None:
        raw = self._record("POST")
        parsed = urlsplit(self.path)
        path = parsed.path
        state = self.server.state  # type: ignore[attr-defined]

        if path == "/local/gpu-mode":
            doc = json.loads(raw.decode("utf-8"))
            state["gpu_mode"] = doc["mode"]
            self._send_json(200, {"gpu": {"mode": doc["mode"], "hardware_active": doc["mode"] == "auto", "mutable": True}})
            return
        if path == "/local/presets":
            doc = json.loads(raw.decode("utf-8"))
            state["presets"].insert(0, {"name": doc["name"], "settings": doc["settings"]})
            self._send_json(201, {"presets": state["presets"]})
            return
        if path == "/v1/render":
            self._send_bytes(200, b"\x89PNG\r\n\x1a\nrendered", "image/png", headers={"Content-Disposition": 'attachment; filename="rendered.png"'})
            return
        if path == "/v1/jobs":
            self._send_json(202, {"id": "job-async-1", "status": "queued", "status_url": "/v1/jobs/job-async-1"})
            return
        if path == "/v1/diff":
            passed = state.get("diff_passed", True)
            ratio = 0.0 if passed else 0.12
            bundle = _make_diff_zip(passed=passed, ratio=ratio)
            self._send_bytes(
                200,
                bundle,
                "application/zip",
                headers={
                    "X-ViperCapture-Diff-Passed": str(passed).lower(),
                    "X-ViperCapture-Difference-Ratio": f"{ratio:.8f}",
                },
            )
            return
        if path.startswith("/v1/baselines/") and path.endswith("/compare"):
            passed = state.get("diff_passed", True)
            ratio = 0.0 if passed else 0.25
            bundle = _make_diff_zip(passed=passed, ratio=ratio)
            self._send_bytes(
                200,
                bundle,
                "application/zip",
                headers={
                    "X-ViperCapture-Diff-Passed": str(passed).lower(),
                    "X-ViperCapture-Difference-Ratio": f"{ratio:.8f}",
                },
            )
            return
        if path == "/v1/jobs/bulk":
            doc = json.loads(raw.decode("utf-8"))
            items = doc.get("items", [])
            results = []
            for idx, item in enumerate(items):
                jid = f"bulk-job-{idx + 1}"
                results.append({
                    "index": idx,
                    "id": item.get("id"),
                    "status": 202,
                    "accepted": True,
                    "job": {
                        "id": jid,
                        "status": "queued",
                        "status_url": f"/v1/jobs/{jid}",
                        "result_url": None,
                    },
                    "error": None,
                })
            self._send_json(200, {"count": len(results), "accepted": len(results), "failed": 0, "results": results})
            return
        if path == "/v1/schedules":
            doc = json.loads(raw.decode("utf-8"))
            sid = "11111111-1111-4111-8111-111111111111"
            record = {
                "id": sid,
                "name": doc["name"],
                "cron": doc["cron"],
                "timezone": doc.get("timezone", "UTC"),
                "enabled": doc.get("enabled", True),
                "next_run_at": "2026-11-24T19:00:00+00:00",
            }
            state["schedules"][sid] = record
            self._send_json(201, record)
            return
        if path == "/v1/profiles":
            doc = json.loads(raw.decode("utf-8"))
            self._send_json(201, {"id": "prof_created_1", "expires_in": doc.get("ttl_seconds")})
            return
        if path == "/v1/profiles/import":
            doc = json.loads(raw.decode("utf-8"))
            self._send_json(201, {
                "id": "prof_imported_1",
                "expires_in": doc.get("ttl_seconds"),
                "cookies": 2,
                "origins": 0,
            })
            return
        if path == "/v1/signed-url":
            qs = parse_qs(parsed.query)
            ttl = int(qs.get("ttl_seconds", ["3600"])[0])
            self._send_json(200, {
                "url": f"/v1/render/signed?payload=abc&expires={1700000000 + ttl}&signature=deadbeef",
                "expires": 1700000000 + ttl,
            })
            return
        if path == "/v1/admin/projects":
            doc = json.loads(raw.decode("utf-8"))
            proj = {
                "id": "proj_1",
                "name": doc["name"],
                "requests_per_minute": doc.get("requests_per_minute", 60),
                "concurrency": doc.get("concurrency", 2),
                "created_at": 1700000000,
            }
            state["projects"].append(proj)
            self._send_json(201, proj)
            return
        if path.startswith("/v1/admin/projects/") and path.endswith("/keys"):
            doc = json.loads(raw.decode("utf-8"))
            pid = path.split("/")[4]
            self._send_json(201, {
                "id": "key_1",
                "project_id": pid,
                "name": doc.get("name", "default"),
                "prefix": "abcd1234",
                "scopes": doc.get("scopes", ["render"]),
                "api_key": "vcp_abcd1234_secretvalue",
            })
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "Not found"}})

    def do_PUT(self) -> None:
        self._record("PUT")
        path = urlsplit(self.path).path
        state = self.server.state  # type: ignore[attr-defined]
        if path.startswith("/v1/baselines/"):
            name = path.split("/")[3]
            doc = {"name": name, "sha256": "a" * 64, "bytes": 123, "updated_at": 1700000000}
            state["baselines"][name] = doc
            self._send_json(201, doc)
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "Not found"}})

    def do_PATCH(self) -> None:
        raw = self._record("PATCH")
        path = urlsplit(self.path).path
        state = self.server.state  # type: ignore[attr-defined]
        if path.startswith("/v1/schedules/"):
            sid = path.split("/")[3]
            doc = json.loads(raw.decode("utf-8"))
            rec = state["schedules"].get(sid)
            if rec is None:
                self._send_json(404, {"error": {"code": "schedule_not_found", "message": "Not found"}})
                return
            for key in ("name", "cron", "timezone", "enabled"):
                if key in doc:
                    rec[key] = doc[key]
            self._send_json(200, rec)
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "Not found"}})

    def do_DELETE(self) -> None:
        self._record("DELETE")
        path = urlsplit(self.path).path
        state = self.server.state  # type: ignore[attr-defined]
        if path.startswith("/local/presets/"):
            name = path.split("/")[3]
            state["presets"] = [p for p in state["presets"] if p["name"] != name]
            self._send_json(200, {"presets": state["presets"]})
            return
        if path.startswith("/v1/baselines/"):
            name = path.split("/")[3]
            state["baselines"].pop(name, None)
            self.send_response(204)
            self.end_headers()
            return
        if path.startswith("/v1/jobs/"):
            jid = path.split("/")[3]
            self._send_json(200, {"id": jid, "status": "cancelled"})
            return
        if path.startswith("/v1/schedules/"):
            sid = path.split("/")[3]
            state["schedules"].pop(sid, None)
            self.send_response(204)
            self.end_headers()
            return
        if path.startswith("/v1/profiles/"):
            self.send_response(204)
            self.end_headers()
            return
        if path.startswith("/v1/admin/keys/"):
            self.send_response(204)
            self.end_headers()
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "Not found"}})

    def log_message(self, format: str, *args) -> None:
        return


class CliCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cert_secret = "test-certification-secret-at-least-32-bytes-long!"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ApiMockHandler)
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.httpd.state = {  # type: ignore[attr-defined]
            "ready": True,
            "gpu_mode": "off",
            "presets": [],
            "baselines": {},
            "schedules": {},
            "projects": [],
            "job_polls": {},
            "diff_passed": True,
            "cert_public_key": "",
        }
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.server = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
            mock.patch("vipercapture.cli_commands.load_local_env"),
            mock.patch("vipercapture.capture_cli.is_local_default", return_value=False),
        ):
            code = cli_commands.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_version_plain_and_json(self) -> None:
        expected = read_version()
        code, stdout, stderr = self._run(["version"])
        self.assertEqual(code, 0, stderr)
        self.assertIn(expected, stdout)
        code, stdout, stderr = self._run(["version", "--json"])
        self.assertEqual(code, 0, stderr)
        doc = json.loads(stdout)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["version"], expected)

    def test_status_running_warming_and_stopped(self) -> None:
        code, stdout, stderr = self._run(["status", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        doc = json.loads(stdout)
        self.assertTrue(doc["running"])
        self.assertTrue(doc["ready"])
        self.assertEqual(doc["cli_version"], read_version())
        self.assertIsNone(doc["server_version"])
        self.assertEqual(doc["config"]["gpu"]["mode"], "off")

        self.httpd.state["ready"] = False  # type: ignore[attr-defined]
        code, stdout, stderr = self._run(["status", "--server", self.server])
        self.assertEqual(code, 0, stderr)
        self.assertIn("warming up", stdout)
        self.assertIn("Engines:", stdout)

        code, stdout, _stderr = self._run(["status", "--server", "http://127.0.0.1:1", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout)["running"])

    def test_logs_path_tail_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "requests.log"
            with mock.patch.dict(os.environ, {"VIPERCAPTURE_LOG_PATH": str(log_file)}):
                code, stdout, _stderr = self._run(["logs", "--path"])
                self.assertEqual(code, 0)
                self.assertEqual(stdout.strip(), str(log_file))

                code, _stdout, stderr = self._run(["logs"])
                self.assertEqual(code, 1)
                self.assertIn("Log file not found", stderr)

                log_file.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
                code, stdout, stderr = self._run(["logs", "--lines", "2"])
                self.assertEqual(code, 0, stderr)
                self.assertEqual(stdout, "line3\nline4\n")

                code, stdout, stderr = self._run(["logs", "--lines", "0"])
                self.assertEqual(code, 0, stderr)
                self.assertEqual(stdout, "line1\nline2\nline3\nline4\n")

    def test_gpu_status_and_switch(self) -> None:
        code, stdout, stderr = self._run(["gpu", "status", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["gpu"]["mode"], "off")

        code, stdout, stderr = self._run(["gpu", "auto", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["gpu"]["mode"], "auto")
        self.assertTrue(json.loads(stdout)["gpu"]["hardware_active"])

        code, stdout, stderr = self._run(["gpu", "off", "--server", self.server])
        self.assertEqual(code, 0, stderr)
        self.assertIn("GPU mode: off", stdout)

    def test_preset_crud_and_secret_redaction(self) -> None:
        code, stdout, stderr = self._run([
            "preset", "save", "dark-desktop",
            "--viewport", "1440", "900",
            "--no-full-page",
            "--color-scheme", "dark",
            "--header", "Authorization: Bearer super-secret-hdr",
            "--server", self.server,
            "--json",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("super-secret-hdr", stdout)
        saved_headers = json.loads(json.loads(stdout)["settings"]["headers"])
        self.assertEqual(saved_headers["Authorization"], "***")

        # Web UI format preset with JSON-string headers (including X-Access-Token) must redact on show & list
        self.httpd.state["presets"].append({  # type: ignore[attr-defined]
            "name": "webui-secret",
            "settings": {
                "output": "png",
                "headers": json.dumps({
                    "Authorization": "Bearer webui-secret-tok",
                    "Cookie": "sid=cookie-secret",
                    "X-Access-Token": "access-secret",
                    "X-Trace": "visible",
                }),
            },
        })
        code, stdout, stderr = self._run(["preset", "list", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("webui-secret-tok", stdout)
        self.assertNotIn("cookie-secret", stdout)
        self.assertNotIn("access-secret", stdout)
        self.assertIn("visible", stdout)

        code, stdout, stderr = self._run(["preset", "show", "webui-secret", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("webui-secret-tok", stdout)
        self.assertNotIn("cookie-secret", stdout)
        self.assertNotIn("access-secret", stdout)
        shown_hdrs = json.loads(json.loads(stdout)["settings"]["headers"])
        self.assertEqual(shown_hdrs["Authorization"], "***")
        self.assertEqual(shown_hdrs["X-Access-Token"], "***")
        self.assertEqual(shown_hdrs["X-Trace"], "visible")

        code, stdout, stderr = self._run(["preset", "list", "--server", self.server])
        self.assertEqual(code, 0, stderr)
        self.assertIn("dark-desktop", stdout)

        code, stdout, stderr = self._run(["preset", "show", "dark-desktop", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("super-secret-hdr", stdout)
        shown_settings = json.loads(stdout)["settings"]
        self.assertEqual(shown_settings["width"], 1440)
        self.assertEqual(shown_settings["height"], 900)
        self.assertFalse(shown_settings["fullPage"])
        self.assertEqual(shown_settings["colorScheme"], "dark")
        # Round-trip back to CLI RenderRequest shape
        from vipercapture.capture_cli import preset_settings_to_payload
        rt = preset_settings_to_payload(shown_settings)
        self.assertEqual(rt["viewport"], {"width": 1440, "height": 900})
        self.assertFalse(rt["full_page"])
        self.assertEqual(rt["environment"]["color_scheme"], "dark")

        code, stdout, stderr = self._run(["preset", "delete", "dark-desktop", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["deleted"], "dark-desktop")

        code, _stdout, stderr = self._run(["preset", "show", "missing", "--server", self.server])
        self.assertEqual(code, 1)
        self.assertIn("not found", stderr)

    def test_diff_uploads_multipart_and_saves_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.png"
            b = Path(tmp) / "b.png"
            out = Path(tmp) / "diff.zip"
            a.write_bytes(b"\x89PNG\r\n\x1a\nA")
            b.write_bytes(b"\x89PNG\r\n\x1a\nB")

            code, stdout, stderr = self._run([
                "diff", str(a), str(b),
                "--output", str(out),
                "--threshold", "5",
                "--max-diff-ratio", "0.05",
                "--server", self.server,
                "--json",
            ])
            self.assertEqual(code, 0, stderr)
            doc = json.loads(stdout)
            self.assertTrue(doc["passed"])
            self.assertTrue(out.is_file())
            req = self.httpd.requests[-1]  # type: ignore[attr-defined]
            self.assertIn("multipart/form-data", req["headers"]["Content-Type"])
            self.assertIn(b'name="baseline"', req["body"])
            self.assertIn(b'name="current"', req["body"])
            self.assertIn(b'name="pixel_threshold"\r\n\r\n5', req["body"])

            self.httpd.state["diff_passed"] = False  # type: ignore[attr-defined]
            code, stdout, _stderr = self._run([
                "diff", str(a), str(b),
                "--fail-on-diff",
                "--server", self.server,
                "--json",
            ])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(stdout)["passed"])

    def test_baseline_save_list_compare_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "shot.png"
            out = Path(tmp) / "cmp.zip"
            img.write_bytes(b"\x89PNG\r\n\x1a\nSHOT")

            code, stdout, stderr = self._run([
                "baseline", "save", "home-page", str(img),
                "--server", self.server,
                "--api-key", "proj-key",
                "--json",
            ])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["name"], "home-page")

            code, stdout, stderr = self._run(["baseline", "list", "--server", self.server, "--json"])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["count"], 1)

            # Compare using live capture (--url) instead of local image
            code, stdout, stderr = self._run([
                "baseline", "compare", "home-page",
                "--url", "https://example.com",
                "--viewport", "1280", "720",
                "--output", str(out),
                "--server", self.server,
                "--json",
            ])
            self.assertEqual(code, 0, stderr)
            self.assertTrue(json.loads(stdout)["passed"])
            self.assertTrue(out.is_file())
            paths = [r["path"] for r in self.httpd.requests[-2:]]  # type: ignore[attr-defined]
            self.assertEqual(paths, ["/v1/render", "/v1/baselines/home-page/compare"])

            code, stdout, stderr = self._run(["baseline", "delete", "home-page", "--server", self.server, "--json"])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["deleted"], "home-page")

            code, _stdout, _stderr = self._run(["baseline", "delete", "../bad", "--server", self.server])
            self.assertEqual(code, 2)

    def test_bulk_submit_and_wait_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            urls_txt = Path(tmp) / "urls.txt"
            urls_txt.write_text("# comment\nhttps://a.example\nhttps://b.example\n", encoding="utf-8")
            out_dir = Path(tmp) / "out"

            code, stdout, stderr = self._run([
                "bulk", str(urls_txt),
                "--idempotency-key", "batch-1",
                "--viewport", "800", "600",
                "--wait",
                "--poll-ms", "1",
                "--concurrency", "2",
                "--output-dir", str(out_dir),
                "--server", self.server,
                "--json",
            ])
            self.assertEqual(code, 0, stderr)
            doc = json.loads(stdout)
            self.assertTrue(doc["ok"])
            self.assertEqual(doc["succeeded"], 2)
            saved_files = sorted(p.name for p in out_dir.iterdir())
            self.assertEqual(len(saved_files), 2)
            post_req = next(r for r in self.httpd.requests if r["path"] == "/v1/jobs/bulk")  # type: ignore[attr-defined]
            self.assertEqual(post_req["headers"].get("Idempotency-Key"), "batch-1")
            sent = json.loads(post_req["body"].decode("utf-8"))
            self.assertEqual(sent["items"][0]["render"]["viewport"]["width"], 800)

            # JSON envelope input without --wait
            envelope = Path(tmp) / "bulk.json"
            envelope.write_text(json.dumps({
                "items": [{"id": "home", "render": {"url": "https://c.example", "output": "png"}}]
            }), encoding="utf-8")
            code, stdout, stderr = self._run(["bulk", str(envelope), "--server", self.server, "--json"])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["accepted"], 1)

    def test_job_status_cancel_and_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "res.png"
            code, stdout, stderr = self._run(["job", "status", "job-42", "--server", self.server, "--json"])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["id"], "job-42")

            code, stdout, stderr = self._run([
                "job", "download", "job-42",
                "--wait", "--poll-ms", "1",
                "--output", str(out),
                "--server", self.server,
                "--json",
            ])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(out.read_bytes(), b"ARTIFACT-job-42")
            self.assertEqual(json.loads(stdout)["job_id"], "job-42")

            code, stdout, stderr = self._run(["job", "cancel", "job-42", "--server", self.server, "--json"])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["status"], "cancelled")

    def test_schedule_crud(self) -> None:
        code, stdout, stderr = self._run([
            "schedule", "create",
            "https://example.com",
            "--name", "hourly-home",
            "--cron", "0 * * * *",
            "--timezone", "America/New_York",
            "--disabled",
            "--viewport", "1280", "720",
            "--server", self.server,
            "--json",
        ])
        self.assertEqual(code, 0, stderr)
        created = json.loads(stdout)
        sid = created["id"]
        self.assertEqual(created["name"], "hourly-home")
        self.assertFalse(created["enabled"])

        code, stdout, stderr = self._run(["schedule", "list", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["count"], 1)

        code, stdout, stderr = self._run(["schedule", "get", sid, "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["id"], sid)

        code, stdout, stderr = self._run(["schedule", "update", sid, "--enabled", "--name", "renamed", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(json.loads(stdout)["enabled"])
        self.assertEqual(json.loads(stdout)["name"], "renamed")

        code, _stdout, _stderr = self._run(["schedule", "update", sid, "--server", self.server])
        self.assertEqual(code, 2)

        code, stdout, stderr = self._run(["schedule", "delete", sid, "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["deleted"], sid)

    def test_profile_create_import_delete_never_echoes_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(json.dumps({
                "cookies": [{
                    "name": "session",
                    "value": "TOP_SECRET_COOKIE_VALUE",
                    "domain": "example.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }],
                "origins": [],
            }), encoding="utf-8")

            code, stdout, stderr = self._run([
                "profile", "create", "--file", str(state_file), "--ttl", "3600",
                "--server", self.server, "--json",
            ])
            self.assertEqual(code, 0, stderr)
            self.assertNotIn("TOP_SECRET_COOKIE_VALUE", stdout)
            self.assertNotIn("TOP_SECRET_COOKIE_VALUE", stderr)
            self.assertEqual(json.loads(stdout)["id"], "prof_created_1")

            cookie_hdr = Path(tmp) / "hdr.txt"
            cookie_hdr.write_text("session=ANOTHER_SECRET_VALUE; theme=dark", encoding="utf-8")
            code, stdout, stderr = self._run([
                "profile", "import",
                "--file", str(cookie_hdr),
                "--format", "cookie_header",
                "--origin", "https://example.com",
                "--server", self.server,
                "--json",
            ])
            self.assertEqual(code, 0, stderr)
            self.assertNotIn("ANOTHER_SECRET_VALUE", stdout)
            self.assertNotIn("ANOTHER_SECRET_VALUE", stderr)
            self.assertEqual(json.loads(stdout)["id"], "prof_imported_1")

            code, stdout, stderr = self._run(["profile", "delete", "prof_imported_1", "--server", self.server, "--json"])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["deleted"], "prof_imported_1")

    def test_sign_uses_signing_token_and_redacts_it(self) -> None:
        code, stdout, stderr = self._run([
            "sign", "https://example.com",
            "--viewport", "1200", "630",
            "--ttl", "900",
            "--signing-token", "super-signing-secret-token",
            "--server", self.server,
            "--json",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("super-signing-secret-token", stdout)
        self.assertNotIn("super-signing-secret-token", stderr)
        doc = json.loads(stdout)
        self.assertIn("/v1/render/signed?", doc["url"])
        req = self.httpd.requests[-1]  # type: ignore[attr-defined]
        self.assertEqual(req["headers"]["Authorization"], "Bearer super-signing-secret-token")
        self.assertIn("ttl_seconds=900", req["path"])

    def test_verify_real_certified_bundle_and_detects_tampering(self) -> None:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from vipercapture.render_engine import RenderArtifact, _certify_artifact, certification_public_key
        except ImportError:
            self.skipTest("cryptography or render_engine dependencies not installed")

        artifact = RenderArtifact(b"\x89PNG\r\n\x1a\ncertified-image", "image/png", "capture.png")
        bundle_bytes = _certify_artifact(artifact, self.cert_secret)
        pub_b64 = certification_public_key(self.cert_secret)
        self.httpd.state["cert_public_key"] = pub_b64  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = Path(tmp) / "certified.zip"
            bundle_path.write_bytes(bundle_bytes)

            # 1. Verify via server /v1/certification/public-key
            code, stdout, stderr = self._run(["verify", str(bundle_path), "--server", self.server, "--json"])
            self.assertEqual(code, 0, stderr)
            doc = json.loads(stdout)
            self.assertTrue(doc["verified"])
            self.assertEqual(doc["artifact"]["filename"], "capture.png")

            # 2. Verify via inline base64 --public-key
            code, stdout, stderr = self._run(["verify", str(bundle_path), "--public-key", pub_b64])
            self.assertEqual(code, 0, stderr)
            self.assertIn("VERIFIED: capture.png", stdout)

            # 3. Verify via PEM public key file
            raw_pub = base64.urlsafe_b64decode(pub_b64 + "=" * (-len(pub_b64) % 4))
            pem = Ed25519PublicKey.from_public_bytes(raw_pub).public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            pem_file = Path(tmp) / "pub.pem"
            pem_file.write_bytes(pem)
            code, _stdout, stderr = self._run(["verify", str(bundle_path), "--public-key", str(pem_file)])
            self.assertEqual(code, 0, stderr)

            # 4. Wrong key fails
            other_pub = certification_public_key("a-completely-different-secret-32-bytes!!")
            code, _stdout, stderr = self._run(["verify", str(bundle_path), "--public-key", other_pub])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stderr)["code"], "public_key_mismatch")

            # 5. Tampered artifact bytes fail SHA-256 check
            tampered = Path(tmp) / "tampered.zip"
            with zipfile.ZipFile(bundle_path, "r") as zin, zipfile.ZipFile(tampered, "w") as zout:
                for info in zin.infolist():
                    data = zin.read(info.filename)
                    if info.filename == "capture.png":
                        data = data + b"X"
                    zout.writestr(info, data)
            code, _stdout, stderr = self._run(["verify", str(tampered), "--public-key", pub_b64])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stderr)["code"], "artifact_tampered")

            # 6. Extra uncertified entry fails
            extra_zip = Path(tmp) / "extra.zip"
            with zipfile.ZipFile(bundle_path, "r") as zin, zipfile.ZipFile(extra_zip, "w") as zout:
                for info in zin.infolist():
                    zout.writestr(info, zin.read(info.filename))
                zout.writestr("injected.txt", b"surprise")
            code, _stdout, stderr = self._run(["verify", str(extra_zip), "--public-key", pub_b64])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stderr)["code"], "bundle_tampered")

    def test_admin_commands(self) -> None:
        token = "admin-secret-token-value"
        code, stdout, stderr = self._run([
            "admin", "projects", "create", "Acme", "--rpm", "120", "--concurrency", "4",
            "--server", self.server, "--admin-token", token, "--json",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertNotIn(token, stdout)
        self.assertEqual(json.loads(stdout)["name"], "Acme")

        code, stdout, stderr = self._run(["admin", "projects", "list", "--server", self.server, "--admin-token", token, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["count"], 1)

        code, stdout, stderr = self._run([
            "admin", "keys", "create", "proj_1", "--name", "ci", "--scope", "render", "--scope", "jobs",
            "--server", self.server, "--admin-token", token, "--json",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertNotIn(token, stdout)
        self.assertEqual(json.loads(stdout)["scopes"], ["render", "jobs"])

        code, stdout, stderr = self._run(["admin", "keys", "revoke", "key_1", "--server", self.server, "--admin-token", token, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["revoked"], "key_1")

        code, stdout, stderr = self._run(["admin", "audit", "--limit", "25", "--server", self.server, "--admin-token", token, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["count"], 1)

        code, stdout, stderr = self._run(["admin", "status", "--server", self.server, "--admin-token", token, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(json.loads(stdout)["browser_connected"])

    def test_completion_generates_all_shells(self) -> None:
        for shell, marker in (
            ("bash", "complete -F _vipercapture_completions vipercapture"),
            ("zsh", "#compdef vipercapture"),
            ("fish", "complete -c vipercapture"),
            ("powershell", "Register-ArgumentCompleter"),
        ):
            with self.subTest(shell=shell):
                code, stdout, stderr = self._run(["completion", shell])
                self.assertEqual(code, 0, stderr)
                self.assertIn(marker, stdout)
                self.assertIn("baseline", stdout)
                self.assertIn("cookie-file", stdout)


class CliConfigTests(unittest.TestCase):
    def test_loads_top_level_and_capture_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            cfg.write_text(
                'server = "http://127.0.0.1:9001"\n'
                "\n[capture]\n"
                'format = "webp"\n'
                "viewport = [1440, 900]\n"
                'color-scheme = "dark"\n'
                "full_page = false\n"
                "retries = 2\n",
                encoding="utf-8",
            )
            loaded = load_cli_config(cfg)
            self.assertEqual(loaded["server"], "http://127.0.0.1:9001")
            self.assertEqual(loaded["format"], "webp")
            self.assertEqual(loaded["viewport"], [1440, 900])
            self.assertEqual(loaded["color_scheme"], "dark")
            self.assertFalse(loaded["full_page"])
            self.assertEqual(loaded["retries"], 2)

    def test_missing_config_returns_empty_and_bad_toml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_cli_config(Path(tmp) / "nope.toml"), {})
            bad = Path(tmp) / "bad.toml"
            bad.write_text("viewport = [1280]\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cli_config(bad)
            table_server = Path(tmp) / "table_server.toml"
            table_server.write_text('api_key = "secret"\n[server]\nurl = "https://example.com"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cli_config(table_server)

    def test_toml_fallback_handles_multiline_arrays_and_trailing_commas(self) -> None:
        doc = _fallback_toml_loads(
            '[capture]\n'
            'viewport = [\n'
            '  1920,\n'
            '  1080,\n'
            ']\n'
            'format = "png"\n'
        )
        self.assertEqual(doc["capture"]["viewport"], [1920, 1080])  # type: ignore[index]
        self.assertEqual(doc["capture"]["format"], "png")  # type: ignore[index]


class CliCouncilHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ApiMockHandler)
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.httpd.state = {  # type: ignore[attr-defined]
            "ready": True,
            "gpu_mode": "off",
            "presets": [],
            "baselines": {},
            "schedules": {},
            "projects": [],
            "job_polls": {},
            "diff_passed": True,
            "cert_public_key": "",
        }
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.server = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
            mock.patch("vipercapture.cli_commands.load_local_env"),
            mock.patch("vipercapture.capture_cli.is_local_default", return_value=False),
        ):
            code = cli_commands.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_status_never_sends_authorization_headers(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"VIPERCAPTURE_ADMIN_TOKEN": "admin-secret", "VIPERCAPTURE_API_KEY": "api-secret"},
            clear=False,
        ):
            code, stdout, stderr = self._run(["status", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(json.loads(stdout)["running"])
        for req in self.httpd.requests:  # type: ignore[attr-defined]
            self.assertNotIn("Authorization", req["headers"])

    def test_companion_command_uses_api_key_from_config_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            cfg.write_text('api_key = "cfg-project-key"\n', encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"VIPERCAPTURE_CONFIG": str(cfg), "VIPERCAPTURE_API_KEY": "", "VIPERCAPTURE_ADMIN_TOKEN": ""},
                clear=False,
            ):
                code, _stdout, stderr = self._run(["baseline", "list", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.httpd.requests[-1]["headers"].get("Authorization"), "Bearer cfg-project-key")  # type: ignore[attr-defined]

    def test_bulk_deduplicates_colliding_ids_and_detects_no_full_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = Path(tmp) / "bulk.json"
            envelope.write_text(
                json.dumps({
                    "items": [
                        {"id": "dup/item", "render": {"url": "https://a.example", "output": "png"}},
                        {"id": "dup:item", "render": {"url": "https://b.example", "output": "png"}},
                    ]
                }),
                encoding="utf-8",
            )
            out_dir = Path(tmp) / "out"
            code, stdout, stderr = self._run([
                "bulk", str(envelope),
                "--no-full-page",
                "--wait", "--poll-ms", "1",
                "--output-dir", str(out_dir),
                "--server", self.server,
                "--json",
            ])
            self.assertEqual(code, 0, stderr)
            doc = json.loads(stdout)
            self.assertEqual(doc["succeeded"], 2)
            saved_paths = [r["path"] for r in doc["results"]]
            self.assertEqual(len(set(saved_paths)), 2)
            post_req = next(r for r in self.httpd.requests if r["path"] == "/v1/jobs/bulk")  # type: ignore[attr-defined]
            sent = json.loads(post_req["body"].decode("utf-8"))
            self.assertFalse(sent["items"][0]["render"]["full_page"])

    def test_diff_rejects_oversized_report_json(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("report.json", "{" + (" " * (cli_commands._MAX_DIFF_REPORT_BYTES + 1024)) + "}")
        with self.assertRaises(cli_commands.CaptureError) as ctx:
            cli_commands._parse_diff_zip_report(buf.getvalue())
        self.assertEqual(ctx.exception.code, "diff_report_too_large")

    def test_verify_server_key_does_not_probe_filesystem_and_rejects_oversized_manifest(self) -> None:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self.skipTest("cryptography not installed")

        with tempfile.TemporaryDirectory() as tmp:
            # Server-supplied key that looks like a file path must NOT call Path.is_file()
            with mock.patch.object(Path, "is_file", side_effect=AssertionError("should not probe path")):
                try:
                    cli_commands._load_ed25519_public_key(r"\\attacker\share\key.pem", allow_file=False)
                except ValueError:
                    pass
                except cli_commands.CaptureError:
                    pass

            # Oversized manifest.json must be rejected cleanly
            bad_zip = Path(tmp) / "bomb.zip"
            with zipfile.ZipFile(bad_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", b"{" + b" " * (cli_commands._MAX_MANIFEST_BYTES + 2048) + b"}")
                zf.writestr("manifest.sig", b"A" * 64)
                zf.writestr("capture.png", b"png")
            pub_raw = Ed25519PrivateKey.generate().public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
            pub_b64 = base64.urlsafe_b64encode(pub_raw).decode("ascii").rstrip("=")
            with self.assertRaises(cli_commands.CaptureError) as ctx:
                cli_commands.verify_certified_bundle(bad_zip, public_key_spec=pub_b64)
            self.assertEqual(ctx.exception.code, "bundle_invalid")

    def test_round2_credential_scoping_long_bulk_ids_and_verify_auth(self) -> None:
        # 1. admin and sign must NOT send local env secrets to non-local servers; sign honors --api-key first
        with mock.patch.dict(
            os.environ,
            {
                "VIPERCAPTURE_ADMIN_TOKEN": "local-admin-secret",
                "VIPERCAPTURE_SIGNING_SECRET": "local-hmac-secret",
                "VIPERCAPTURE_SIGNING_ADMIN_TOKEN": "local-sign-admin",
                "VIPERCAPTURE_API_KEY": "",
            },
            clear=False,
        ):
            code, _out, stderr = self._run(["admin", "status", "--server", self.server, "--json"])
            self.assertEqual(code, 0, stderr)
            self.assertNotIn("Authorization", self.httpd.requests[-1]["headers"])  # type: ignore[attr-defined]

            code, _out, stderr = self._run([
                "sign", "https://example.com",
                "--server", self.server,
                "--api-key", "explicit-proj-key",
                "--json",
            ])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(
                self.httpd.requests[-1]["headers"].get("Authorization"),  # type: ignore[attr-defined]
                "Bearer explicit-proj-key",
            )

            # gpu status probes /app-config anonymously (no Authorization)
            code, _out, stderr = self._run(["gpu", "status", "--server", self.server, "--api-key", "proj-key", "--json"])
            self.assertEqual(code, 0, stderr)
            self.assertNotIn("Authorization", self.httpd.requests[-1]["headers"])  # type: ignore[attr-defined]

        # 2. config.toml api_key scoped to configured server origin is NOT sent to a different --server
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            cfg.write_text('server = "https://other.example"\napi_key = "other-origin-key"\n', encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"VIPERCAPTURE_CONFIG": str(cfg), "VIPERCAPTURE_API_KEY": "", "VIPERCAPTURE_ADMIN_TOKEN": ""},
                clear=False,
            ):
                code, _out, stderr = self._run(["baseline", "list", "--server", self.server, "--json"])
            self.assertEqual(code, 0, stderr)
            self.assertNotIn("Authorization", self.httpd.requests[-1]["headers"])  # type: ignore[attr-defined]

        # 3. verify --api-key sends Authorization header when fetching /v1/certification/public-key
        with tempfile.TemporaryDirectory() as tmp:
            dummy = Path(tmp) / "dummy.zip"
            dummy.write_bytes(b"not-a-zip")
            self.httpd.state["cert_public_key"] = "A" * 43  # type: ignore[attr-defined]
            self._run(["verify", str(dummy), "--server", self.server, "--api-key", "verify-proj-key"])
            cert_req = next(r for r in reversed(self.httpd.requests) if r["path"] == "/v1/certification/public-key")  # type: ignore[attr-defined]
            self.assertEqual(cert_req["headers"].get("Authorization"), "Bearer verify-proj-key")

        # 4. bulk --wait with duplicate 128-character IDs terminates cleanly with unique filenames
        with tempfile.TemporaryDirectory() as tmp:
            long_id = "a" * 128
            envelope = Path(tmp) / "long_bulk.json"
            envelope.write_text(
                json.dumps({
                    "items": [
                        {"id": long_id, "render": {"url": "https://a.example", "output": "png"}},
                        {"id": long_id, "render": {"url": "https://b.example", "output": "png"}},
                    ]
                }),
                encoding="utf-8",
            )
            out_dir = Path(tmp) / "out_long"
            code, stdout, stderr = self._run([
                "bulk", str(envelope),
                "--wait", "--poll-ms", "1",
                "--output-dir", str(out_dir),
                "--server", self.server,
                "--json",
            ])
            self.assertEqual(code, 0, stderr)
            doc = json.loads(stdout)
            self.assertEqual(doc["succeeded"], 2)
            self.assertEqual(len({r["path"] for r in doc["results"]}), 2)

    def test_round5_status_server_version_and_preset_bool_strings(self) -> None:
        # 1. status reports server_version from /app-config when distinct from CLI version
        self.httpd.state["app_config_version"] = "9.9.9-server"  # type: ignore[attr-defined]
        code, stdout, stderr = self._run(["status", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        doc = json.loads(stdout)
        self.assertEqual(doc["cli_version"], read_version())
        self.assertEqual(doc["server_version"], "9.9.9-server")
        self.assertNotEqual(doc["cli_version"], doc["server_version"])

        code, text_out, stderr = self._run(["status", "--server", self.server])
        self.assertEqual(code, 0, stderr)
        self.assertIn(f"CLI Version: {read_version()}", text_out)
        self.assertIn("Server Ver:  9.9.9-server", text_out)

        # 2. status falls back to /openapi.json info.version when /app-config omits version
        self.httpd.state["app_config_version"] = None  # type: ignore[attr-defined]
        self.httpd.state["openapi_version"] = "8.8.8-openapi"  # type: ignore[attr-defined]
        code, stdout, stderr = self._run(["status", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        doc = json.loads(stdout)
        self.assertEqual(doc["cli_version"], read_version())
        self.assertEqual(doc["server_version"], "8.8.8-openapi")

        # 3. preset save --from-json coerces false/0 boolean strings across image, diagnostics, video, cleanup
        with tempfile.TemporaryDirectory() as tmp:
            img_json = Path(tmp) / "img_false.json"
            img_json.write_text(
                json.dumps({
                    "output": "png",
                    "image": {
                        "transparent_background": "false",
                        "optimize_for_speed": "0",
                    },
                    "diagnostics": {"bundle": "false"},
                    "cleanup": {
                        "block_ads": "false",
                        "block_trackers": "0",
                    },
                }),
                encoding="utf-8",
            )
            code, stdout, stderr = self._run([
                "preset", "save", "false-strings-img",
                "--from-json", str(img_json),
                "--server", self.server,
                "--json",
            ])
            self.assertEqual(code, 0, stderr)
            saved = json.loads(stdout)["settings"]
            self.assertIs(saved["transparent"], False)
            self.assertIs(saved["optimizePng"], False)
            self.assertIs(saved["diagnostics"], False)
            self.assertIs(saved["blockAds"], False)
            self.assertIs(saved["blockTrackers"], False)

            vid_json = Path(tmp) / "vid_false.json"
            vid_json.write_text(
                json.dumps({
                    "output": "webm",
                    "video": {
                        "scroll": "false",
                        "transparent_background": "0",
                    },
                }),
                encoding="utf-8",
            )
            code, stdout, stderr = self._run([
                "preset", "save", "false-strings-vid",
                "--from-json", str(vid_json),
                "--server", self.server,
                "--json",
            ])
            self.assertEqual(code, 0, stderr)
            saved_vid = json.loads(stdout)["settings"]
            self.assertIs(saved_vid["videoScroll"], False)
            self.assertIs(saved_vid["transparent"], False)

    def test_round6_crop_presets_bulk_timezone_template_copy_extract_archive_and_openapi_status(self) -> None:
        from vipercapture import capture_cli

        # 1. Web UI crop presets with fullPage=True force full_page=False and keep selector / clip
        sel_payload = capture_cli.preset_settings_to_payload({
            "output": "png",
            "fullPage": True,
            "selector": "#invoice",
        })
        self.assertIs(sel_payload.get("full_page"), False)
        self.assertEqual(sel_payload.get("selector"), "#invoice")

        clip_payload = capture_cli.preset_settings_to_payload({
            "output": "png",
            "fullPage": True,
            "clipEnabled": True,
            "clipX": 10,
            "clipY": 20,
            "clipWidth": 200,
            "clipHeight": 100,
        })
        self.assertIs(clip_payload.get("full_page"), False)
        self.assertEqual(clip_payload.get("clip"), {"x": 10.0, "y": 20.0, "width": 200.0, "height": 100.0})

        # 2. Preset replay preserves extract_mode for side_outputs containing markdown/html
        side_payload = capture_cli.preset_settings_to_payload({
            "output": "png",
            "side_outputs": ["markdown"],
            "extractMode": "article",
        })
        self.assertEqual(side_payload.get("extract_mode"), "article")

        # 3. Direct JSON diagnostics.bundle = "false" is NOT treated as a zip archive
        self.assertFalse(capture_cli.artifact_is_archive({"output": "png", "diagnostics": {"bundle": "false"}}))
        self.assertFalse(capture_cli.artifact_is_archive({"output": "png", "certification": {"enabled": "0"}}))

        # 4. bulk --url ... --timezone Asia/Tokyo preserves environment.timezone
        code, _stdout, stderr = self._run([
            "bulk", "--url", "https://example.org",
            "--timezone", "Asia/Tokyo",
            "--server", self.server,
            "--json",
        ])
        self.assertEqual(code, 0, stderr)
        bulk_req = next(r for r in reversed(self.httpd.requests) if r["path"] == "/v1/jobs/bulk")  # type: ignore[attr-defined]
        bulk_body = json.loads(bulk_req["body"].decode("utf-8"))
        self.assertEqual(bulk_body["items"][0]["render"]["environment"]["timezone"], "Asia/Tokyo")

        # 5. Single-URL mistyped --output-template exits 2 cleanly (no uncaught ValueError)
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            code = capture_cli.main([
                "https://example.org",
                "--output-template", "{missing}{ext}",
                "--server", self.server,
            ])
        self.assertEqual(code, 2)
        self.assertIn("missing", buf_err.getvalue())

        # 6. Multi-URL --copy exits 2 before capturing
        with tempfile.TemporaryDirectory() as tmp:
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
                code = capture_cli.main([
                    "https://one.example", "https://two.example",
                    "--copy", "--output-dir", tmp,
                    "--server", self.server,
                ])
            self.assertEqual(code, 2)
            self.assertIn("--copy", buf_err.getvalue())

        # 7. status ignores non-200 /openapi.json error bodies
        self.httpd.state["app_config_version"] = None  # type: ignore[attr-defined]
        self.httpd.state["openapi_version"] = "edge-build"  # type: ignore[attr-defined]
        self.httpd.state["openapi_status"] = 502  # type: ignore[attr-defined]
        code, stdout, stderr = self._run(["status", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertIsNone(json.loads(stdout)["server_version"])

    def test_round7_empty_download_preserves_file_pdf_preset_bulk_dir_template_and_open_count(self) -> None:
        from vipercapture import capture_cli

        # 1. Empty download fails without deleting pre-existing destination file
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "existing.png"
            existing.write_bytes(b"original-artifact")

            class _EmptyResp:
                headers: dict[str, str] = {}

                def read(self, _n: int = -1) -> bytes:
                    return b""

            with self.assertRaises(capture_cli.CaptureError):
                capture_cli.write_response(_EmptyResp(), existing)
            self.assertTrue(existing.is_file())
            self.assertEqual(existing.read_bytes(), b"original-artifact")

        # 2. Saved Web UI PDF presets omit preserve_viewport_width, selector, and clip
        pdf_payload = capture_cli.preset_settings_to_payload({
            "output": "pdf",
            "fullPage": False,
            "preserveViewportWidth": True,
            "selector": "#invoice",
            "clipEnabled": True,
        })
        self.assertNotIn("preserve_viewport_width", pdf_payload)
        self.assertNotIn("selector", pdf_payload)
        self.assertNotIn("clip", pdf_payload)
        capture_cli.validate_payload({**pdf_payload, "url": "https://example.org"})

        # 3. bulk --wait with --output-dir pointing to an existing regular file fails BEFORE submitting
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "existing.txt"
            blocker.write_text("file", encoding="utf-8")
            before_count = len(self.httpd.requests)  # type: ignore[attr-defined]
            code, _stdout, stderr = self._run([
                "bulk", "--url", "https://example.org",
                "--wait", "--json",
                "--output-dir", str(blocker),
                "--server", self.server,
            ])
            self.assertEqual(code, 2)
            self.assertIn("--output-dir", stderr)
            self.assertEqual(len(self.httpd.requests), before_count)  # type: ignore[attr-defined]

        # 4. AttributeError in --output-template ('{host.missing}{ext}') exits 2 cleanly;
        #    --async --no-wait skips template resolution consistently for single-URL captures
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            code = capture_cli.main([
                "https://example.org",
                "--no-config", "--no-start",
                "--output-template", "{host.missing}{ext}",
                "--server", self.server,
            ])
        self.assertEqual(code, 2)
        self.assertIn("Invalid --output-template", buf_err.getvalue())

        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            code = capture_cli.main([
                "https://example.org",
                "--async", "--no-wait",
                "--output-template", "{missing}{ext}",
                "--server", self.server,
                "--json",
            ])
        self.assertEqual(code, 0, buf_err.getvalue())

        # 5. Multi-URL --open failure does NOT double-count results
        with tempfile.TemporaryDirectory() as tmp:
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with (
                mock.patch("sys.stdout", buf_out),
                mock.patch("sys.stderr", buf_err),
                mock.patch.object(
                    capture_cli,
                    "open_saved_file",
                    side_effect=capture_cli.CaptureError("opener failed"),
                ),
            ):
                code = capture_cli.main([
                    "https://one.example", "https://two.example",
                    "--open", "--output-dir", tmp,
                    "--server", self.server,
                    "--json",
                ])
            self.assertEqual(code, 1)
            doc = json.loads(buf_out.getvalue())
            self.assertEqual(doc["count"], 2)
            self.assertEqual(doc["succeeded"], 0)
            self.assertEqual(doc["failed"], 2)

    def test_round8_truncated_download_locked_file_batch_template_typeerror_and_bulk_mkdir_order(self) -> None:
        from vipercapture import capture_cli

        # 1. Truncated download (Content-Length: 100, 9 bytes sent) raises CaptureError
        #    and leaves pre-existing artifact untouched
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "existing.png"
            existing.write_bytes(b"original-good-artifact")

            class _TruncatedResp:
                status = 200
                headers = {"Content-Length": "100"}

                def __init__(self) -> None:
                    self._chunks = [b"123456789", b""]

                def read(self, _n: int = -1) -> bytes:
                    return self._chunks.pop(0) if self._chunks else b""

            with self.assertRaises(capture_cli.CaptureError):
                capture_cli.write_response(_TruncatedResp(), existing)
            self.assertEqual(existing.read_bytes(), b"original-good-artifact")

        # 2. PermissionError writing one artifact in bulk --wait --json still emits full JSON summary
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "bulk_out"
            orig_write = capture_cli.write_response
            call_idx = {"n": 0}

            def _fail_first_write(resp, path):
                call_idx["n"] += 1
                if call_idx["n"] == 1:
                    raise PermissionError("file locked")
                return orig_write(resp, path)

            with mock.patch.object(cli_commands, "write_response", side_effect=_fail_first_write):
                code, stdout, stderr = self._run([
                    "bulk",
                    "--url", "https://one.example",
                    "--url", "https://two.example",
                    "--wait", "--poll-ms", "1",
                    "--concurrency", "1",
                    "--output-dir", str(out_dir),
                    "--server", self.server,
                    "--json",
                ])
            self.assertEqual(code, 1, stderr)
            doc = json.loads(stdout)
            self.assertFalse(doc["ok"])
            self.assertEqual(doc["count"], 2)
            self.assertEqual(doc["succeeded"], 1)
            self.assertEqual(doc["failed"], 1)

        # 3. PermissionError in multi-URL capture --json records per-item error and finishes remaining URLs
        with tempfile.TemporaryDirectory() as tmp:
            orig_write = capture_cli.write_response
            cap_idx = {"n": 0}

            def _fail_first_cap(resp, path):
                cap_idx["n"] += 1
                if cap_idx["n"] == 1:
                    raise PermissionError("file locked")
                return orig_write(resp, path)

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with (
                mock.patch("sys.stdout", buf_out),
                mock.patch("sys.stderr", buf_err),
                mock.patch.object(capture_cli, "write_response", side_effect=_fail_first_cap),
            ):
                code = capture_cli.main([
                    "https://one.example", "https://two.example",
                    "--output-dir", tmp,
                    "--server", self.server,
                    "--json",
                ])
            self.assertEqual(code, 1)
            doc = json.loads(buf_out.getvalue())
            self.assertEqual(doc["count"], 2)
            self.assertEqual(doc["succeeded"], 1)
            self.assertEqual(doc["failed"], 1)

        # 4. Invalid template '{index[0]}{ext}' raises TypeError -> caught and exits 2 cleanly
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            code = capture_cli.main([
                "https://example.org",
                "--no-config", "--no-start",
                "--output-template", "{index[0]}{ext}",
                "--server", self.server,
            ])
        self.assertEqual(code, 2)
        self.assertIn("Invalid --output-template", buf_err.getvalue())

        # 5. Invalid bulk input (no URLs) exits 2 WITHOUT creating --output-dir
        with tempfile.TemporaryDirectory() as tmp:
            new_dir = Path(tmp) / "should_not_exist"
            code, _stdout, _stderr = self._run([
                "bulk", "--wait", "--output-dir", str(new_dir),
            ])
            self.assertEqual(code, 2)
            self.assertFalse(new_dir.exists())

    def test_round9_coalesced_cl_truncated_json_bulk_partial_retries_long_host_and_job_oserror(self) -> None:
        from email.message import Message
        import http.client as _hc
        from vipercapture import capture_cli

        # 1. Coalesced Content-Length: "100, 100" still detects 9-byte truncation and preserves existing file
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "existing.png"
            existing.write_bytes(b"good-file")
            hdrs = Message()
            hdrs["Content-Length"] = "100, 100"

            class _CoalescedResp:
                status = 200
                headers = hdrs

                def __init__(self) -> None:
                    self._chunks = [b"123456789", b""]

                def read(self, _n: int = -1) -> bytes:
                    return self._chunks.pop(0) if self._chunks else b""

            with self.assertRaises(capture_cli.CaptureError):
                capture_cli.write_response(_CoalescedResp(), existing)
            self.assertEqual(existing.read_bytes(), b"good-file")

        # 2. Truncated JSON (declared Content-Length > actual) and chunked IncompleteRead raise CaptureError
        class _ShortJsonResp:
            status = 200
            headers = {"Content-Length": "50"}

            def read(self, _n: int = -1) -> bytes:
                return b'{"id":"1"}'

        with self.assertRaises(capture_cli.CaptureError):
            capture_cli._read_json_value(_ShortJsonResp())

        class _BrokenChunkedResp:
            status = 200
            headers = {"Transfer-Encoding": "chunked"}

            def read(self, _n: int = -1) -> bytes:
                raise _hc.IncompleteRead(b'{"id":')

        with self.assertRaises(capture_cli.CaptureError):
            capture_cli._read_json_value(_BrokenChunkedResp())

        # 3. --retries retries an interrupted artifact download when body is truncated on attempt 1
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "retried.png"
            attempts = {"n": 0}

            class _FlakyResp:
                status = 200

                def __init__(self, truncated: bool) -> None:
                    self.headers = {"Content-Length": "8", "Content-Type": "image/png"}
                    self._chunks = [b"123", b""] if truncated else [b"\x89PNG1234", b""]

                def read(self, _n: int = -1) -> bytes:
                    return self._chunks.pop(0) if self._chunks else b""

                def close(self) -> None:
                    return

            def _fake_open(*_args, **_kwargs):
                attempts["n"] += 1
                return _FlakyResp(truncated=(attempts["n"] == 1))

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with (
                mock.patch("sys.stdout", buf_out),
                mock.patch("sys.stderr", buf_err),
                mock.patch.object(capture_cli, "open_response_with_retries", side_effect=_fake_open),
            ):
                code = capture_cli.main([
                    "https://example.org",
                    "--output", str(target),
                    "--retries", "1",
                    "--retry-delay-ms", "0",
                    "--server", self.server,
                ])
            self.assertEqual(code, 0, buf_err.getvalue())
            self.assertEqual(attempts["n"], 2)
            self.assertEqual(target.read_bytes(), b"\x89PNG1234")

        # 4. Bulk submission of 101 URLs retains the first 100 accepted IDs when chunk 2 returns 503
        with tempfile.TemporaryDirectory() as tmp:
            urls_file = Path(tmp) / "urls.txt"
            urls_file.write_text("\n".join(f"https://u{i}.example" for i in range(101)), encoding="utf-8")
            orig_open = cli_commands.open_response
            bulk_calls = {"n": 0}

            def _fail_second_bulk(url, **kwargs):
                if url.endswith("/v1/jobs/bulk"):
                    bulk_calls["n"] += 1
                    if bulk_calls["n"] == 2:
                        raise capture_cli.CaptureError("Service Unavailable", status=503, code="service_unavailable")
                return orig_open(url, **kwargs)

            with mock.patch.object(cli_commands, "open_response", side_effect=_fail_second_bulk):
                code, stdout, stderr = self._run([
                    "bulk", str(urls_file),
                    "--server", self.server,
                    "--json",
                ])
            self.assertEqual(code, 1, stderr)
            doc = json.loads(stdout)
            self.assertEqual(doc["accepted"], 100)
            self.assertEqual(doc["failed"], 1)
            self.assertEqual(len(doc["results"]), 101)

        # 5. Bulk invalid item (empty id) and invalid --request-timeout -1 exit 2 WITHOUT creating --output-dir
        with tempfile.TemporaryDirectory() as tmp:
            bad_items = Path(tmp) / "bad_items.json"
            bad_items.write_text('[{"id":"","render":{"url":"https://example.org"}}]', encoding="utf-8")
            new_dir_1 = Path(tmp) / "no_create_1"
            code, _stdout, _stderr = self._run([
                "bulk", str(bad_items), "--wait", "--output-dir", str(new_dir_1),
            ])
            self.assertEqual(code, 2)
            self.assertFalse(new_dir_1.exists())

            new_dir_2 = Path(tmp) / "no_create_2"
            code, _stdout, _stderr = self._run([
                "bulk", "--url", "https://example.org",
                "--wait", "--output-dir", str(new_dir_2),
                "--request-timeout", "-1",
            ])
            self.assertEqual(code, 2)
            self.assertFalse(new_dir_2.exists())

        # 6. job download --json returns structured JSON error (exit 1) on OSError instead of traceback
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "existing-file"
            blocker.write_text("x", encoding="utf-8")
            code, _stdout, stderr = self._run([
                "job", "download", "job-1",
                "--output", str(blocker / "result.png"),
                "--server", self.server,
                "--json",
            ])
            self.assertEqual(code, 1)
            err_doc = json.loads(stderr.strip())
            self.assertFalse(err_doc["ok"])

        # 7. Default multi-URL filename preserves unique -{index} even for 89-character hostnames
        long_host = f"{'a' * 40}.{'b' * 40}.example"
        with tempfile.TemporaryDirectory() as tmp:
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
                code = capture_cli.main([
                    f"https://{long_host}/a",
                    f"https://{long_host}/b",
                    "--output-dir", tmp,
                    "--dry-run",
                ])
            self.assertEqual(code, 0, buf_err.getvalue())

        # 8. Bulk validation rejects extra keys & non-string id before coercion/mkdir;
        #    render retries share one unified counter across open and body read
        with tempfile.TemporaryDirectory() as tmp:
            extra_file = Path(tmp) / "extra_field.json"
            extra_file.write_text('[{"id":123,"extra":true,"render":{"url":"https://example.org"}}]', encoding="utf-8")
            no_dir = Path(tmp) / "no_create_extra"
            code, _out, _err = self._run([
                "bulk", str(extra_file), "--wait", "--output-dir", str(no_dir),
            ])
            self.assertEqual(code, 2)
            self.assertFalse(no_dir.exists())

            target_retry = Path(tmp) / "retry_cap.png"
            call_count = {"n": 0}

            class _ShortBodyResp:
                status = 200
                headers = {"Content-Length": "10", "Content-Type": "image/png"}

                def __init__(self) -> None:
                    self._chunks = [b"123", b""]

                def read(self, _n: int = -1) -> bytes:
                    return self._chunks.pop(0) if self._chunks else b""

                def close(self) -> None:
                    return

            def _fail_then_truncate(*_args, **_kwargs):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise capture_cli.CaptureError("503", status=503)
                return _ShortBodyResp()

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with (
                mock.patch("sys.stdout", buf_out),
                mock.patch("sys.stderr", buf_err),
                mock.patch.object(capture_cli, "open_response_with_retries", side_effect=_fail_then_truncate),
            ):
                code = capture_cli.main([
                    "https://example.org",
                    "--output", str(target_retry),
                    "--retries", "1",
                    "--retry-delay-ms", "0",
                    "--server", self.server,
                ])
            self.assertEqual(code, 1)
            self.assertEqual(call_count["n"], 2)

    def test_round11_framing_retries_template_diff_and_timeout_hardening(self) -> None:
        import socket
        import time
        from vipercapture import capture_cli

        # 1. Coalesced Content-Length: 8, 8 on HTTP/1.1 keep-alive returns immediately (both binary & JSON)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        hold_open = threading.Event()
        kept_sockets: list[socket.socket] = []

        def _serve_keepalive() -> None:
            try:
                for payload_bytes, ctype in (
                    (b"ABCDEFGH", "application/octet-stream"),
                    (b'{"ok":1}', "application/json"),
                ):
                    conn, _ = srv.accept()
                    kept_sockets.append(conn)
                    conn.recv(4096)
                    hdr = (
                        f"HTTP/1.1 200 OK\r\n"
                        f"Content-Type: {ctype}\r\n"
                        f"Content-Length: 8, 8\r\n"
                        f"Connection: keep-alive\r\n\r\n"
                    ).encode("ascii")
                    conn.sendall(hdr + payload_bytes)
                hold_open.wait(timeout=3.0)
            finally:
                for sock in kept_sockets:
                    try:
                        sock.close()
                    except OSError:
                        pass
                srv.close()

        t = threading.Thread(target=_serve_keepalive, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out_file = Path(tmp) / "coalesced.bin"
                t0 = time.monotonic()
                resp = capture_cli.open_raw_response(
                    f"http://127.0.0.1:{port}/bin",
                    method="GET",
                    body=None,
                    headers={},
                    timeout=1.5,
                )
                try:
                    n = capture_cli.write_response(resp, out_file)
                finally:
                    resp.close()
                elapsed_bin = time.monotonic() - t0
                self.assertEqual(n, 8)
                self.assertEqual(out_file.read_bytes(), b"ABCDEFGH")
                self.assertLess(elapsed_bin, 0.8)

                t1 = time.monotonic()
                resp_json = capture_cli.open_raw_response(
                    f"http://127.0.0.1:{port}/json",
                    method="GET",
                    body=None,
                    headers={},
                    timeout=1.5,
                )
                try:
                    doc = capture_cli._read_json(resp_json)
                finally:
                    resp_json.close()
                elapsed_json = time.monotonic() - t1
                self.assertEqual(doc, {"ok": 1})
                self.assertLess(elapsed_json, 0.8)
        finally:
            hold_open.set()
            t.join(timeout=2.0)

        # 2. Stalled keep-alive body raises CaptureError and is retried under --retries
        srv2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv2.bind(("127.0.0.1", 0))
        srv2.listen(4)
        port2 = srv2.getsockname()[1]
        stall_done = threading.Event()
        conns2 = {"n": 0}
        sockets2: list[socket.socket] = []

        def _drain_http_request(conn: socket.socket) -> None:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, body = buf.split(b"\r\n\r\n", 1)
            cl = 0
            for line in head.decode("iso-8859-1").splitlines()[1:]:
                if line.lower().startswith("content-length:"):
                    cl = int(line.split(":", 1)[1].strip())
                    break
            while len(body) < cl:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                body += chunk

        def _serve_stall_then_ok() -> None:
            try:
                # Attempt 1: send 9 bytes of 100 and stall
                c1, _ = srv2.accept()
                conns2["n"] += 1
                sockets2.append(c1)
                _drain_http_request(c1)
                c1.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: image/png\r\n"
                    b"Content-Length: 100\r\n"
                    b"Connection: keep-alive\r\n\r\n"
                    b"123456789"
                )
                # Attempt 2: return complete body
                c2, _ = srv2.accept()
                conns2["n"] += 1
                sockets2.append(c2)
                _drain_http_request(c2)
                c2.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: image/png\r\n"
                    b"Content-Length: 8\r\n"
                    b"Connection: close\r\n\r\n"
                    b"\x89PNG\r\n\x1a\n"
                )
                try:
                    c2.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                stall_done.wait(timeout=3.0)
            finally:
                for s in sockets2:
                    try:
                        s.close()
                    except OSError:
                        pass
                srv2.close()

        t2 = threading.Thread(target=_serve_stall_then_ok, daemon=True)
        t2.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out_stall = Path(tmp) / "recovered.png"
                args = capture_cli.parse_capture_args([
                    "https://example.org",
                    "--output", str(out_stall),
                    "--server", f"http://127.0.0.1:{port2}",
                    "--request-timeout", "0.35",
                    "--retries", "1",
                    "--retry-delay-ms", "0",
                    "--no-start",
                    "--no-config",
                ])
                _, total, _ = capture_cli._execute_one(
                    args,
                    {"url": "https://example.org", "output": "png"},
                    root=f"http://127.0.0.1:{port2}",
                    output=out_stall,
                    started_server=False,
                )
                self.assertEqual(conns2["n"], 2)
                self.assertEqual(total, 8)
                self.assertEqual(out_stall.read_bytes(), b"\x89PNG\r\n\x1a\n")
        finally:
            stall_done.set()
            t2.join(timeout=2.0)

        # 3. Unparsable Content-Length token raises CaptureError (never turns completeness check off)
        class _BadClResp:
            status = 200
            headers = {"Content-Length": "100, nope"}

            def read(self, _n: int = -1) -> bytes:
                return b"123456789"

        with self.assertRaises(capture_cli.CaptureError):
            capture_cli._expected_content_length(_BadClResp())

        # 4. Async result downloads retry truncated bodies under unified open+body counter
        with tempfile.TemporaryDirectory() as tmp:
            async_out = Path(tmp) / "async_retry.png"
            calls: list[str] = []

            class _FakeResp:
                def __init__(self, body: bytes, cl: str) -> None:
                    self.status = 200
                    self.headers = {"Content-Length": cl, "Content-Type": "application/json"}
                    self._chunks = [body, b""]

                def read(self, _n: int = -1) -> bytes:
                    return self._chunks.pop(0) if self._chunks else b""

                def close(self) -> None:
                    return

            def _fake_async_open(url, **_kwargs):
                calls.append(url)
                if len(calls) == 1:
                    body = json.dumps({
                        "id": "job-1",
                        "status": "succeeded",
                        "result_url": "/v1/jobs/job-1/result",
                    }).encode("utf-8")
                    return _FakeResp(body, str(len(body)))
                if len(calls) == 2:
                    return _FakeResp(b"1234", "20")
                return _FakeResp(b"01234567890123456789", "20")

            args = capture_cli.parse_capture_args([
                "https://example.org",
                "--async",
                "--output", str(async_out),
                "--retries", "1",
                "--retry-delay-ms", "0",
                "--server", self.server,
                "--no-start",
                "--no-config",
            ])
            with mock.patch.object(capture_cli, "open_response_with_retries", side_effect=_fake_async_open):
                _, total = capture_cli._execute_async(
                    args,
                    {"url": "https://example.org", "output": "png"},
                    root=self.server,
                    output=async_out,
                    timeout=30.0,
                    started_server=False,
                    retries=1,
                    retry_delay_ms=0,
                )
            self.assertEqual(len(calls), 3)
            self.assertEqual(total, 20)
            self.assertEqual(async_out.read_bytes(), b"01234567890123456789")

        # 5. {index} preservation does not collide when truncated slug happens to end in the index digits
        host = "h" * 56
        slug = "a" * 21 + "11" + "b" * 33
        template = "{host}-{slug}-{index}{ext}"
        payload = {"url": f"https://{host}.com/{slug}", "output": "png"}
        f1 = capture_cli.render_output_template(template, payload=payload, index=1)
        f2 = capture_cli.render_output_template(template, payload=payload, index=2)
        f11 = capture_cli.render_output_template(template, payload=payload, index=11)
        self.assertEqual(len({f1, f2, f11}), 3)
        self.assertTrue(f1.endswith("-1.png"), f1)
        self.assertTrue(f11.endswith("-11.png"), f11)
        # Leading {index} survives truncation without duplicate suffix
        lead1 = capture_cli.render_output_template("{index}-{host}-{slug}{ext}", payload=payload, index=1)
        self.assertTrue(lead1.startswith("1-"), lead1)
        self.assertFalse(lead1.endswith("-1.png"), lead1)

        # 6. diff --output under an existing file returns structured JSON error (exit 1, no traceback)
        with tempfile.TemporaryDirectory() as tmp:
            png_stub = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
            p_a = Path(tmp) / "a.png"
            p_b = Path(tmp) / "b.png"
            p_a.write_bytes(png_stub)
            p_b.write_bytes(png_stub)
            blocker = Path(tmp) / "block"
            blocker.write_text("file", encoding="utf-8")
            code, _stdout, stderr = self._run([
                "diff", str(p_a), str(p_b),
                "--output", str(blocker / "out.zip"),
                "--server", self.server,
                "--no-start",
            ])
            self.assertEqual(code, 1)
            err_doc = json.loads(stderr.strip())
            self.assertFalse(err_doc["ok"])

        # 7. Negative/zero/NaN --request-timeout rejected cleanly with exit 2 across commands
        for argv in (
            ["job", "status", "job-1", "--server", self.server, "--no-start", "--request-timeout", "-1"],
            ["job", "status", "job-1", "--server", self.server, "--no-start", "--request-timeout", "nan"],
            ["diff", "a.png", "b.png", "--server", self.server, "--no-start", "--request-timeout", "0"],
            ["baseline", "compare", "home", "a.png", "--server", self.server, "--no-start", "--request-timeout", "inf"],
        ):
            code, _stdout, stderr = self._run(argv)
            self.assertEqual(code, 2, argv)
            err_doc = json.loads(stderr.strip())
            self.assertFalse(err_doc["ok"])

        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            code = capture_cli.main(["https://example.org", "--request-timeout", "-1"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(buf_err.getvalue().strip())["ok"])

    def test_round12_huge_timeout_huge_cl_template_post_ext_wide_index_and_config_overflow(self) -> None:
        from vipercapture import capture_cli, cli_config

        # 1. Timeout >= 2147483.648 rejected cleanly with exit 2 (never OverflowError)
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            code = capture_cli.main([
                "https://example.org",
                "--server", "http://127.0.0.1:9",
                "--no-start",
                "--no-config",
                "--request-timeout", "2147484",
            ])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(buf_err.getvalue().strip())["ok"])

        code2, _out2, err2 = self._run([
            "job", "status", "job-1",
            "--server", self.server,
            "--no-start",
            "--request-timeout", "2147484",
        ])
        self.assertEqual(code2, 2)
        self.assertFalse(json.loads(err2.strip())["ok"])

        # 2. 5000-digit Content-Length raises CaptureError instead of ValueError
        class _HugeClResp:
            status = 200
            headers = {"Content-Length": "9" * 5000}

        with self.assertRaises(capture_cli.CaptureError):
            capture_cli._expected_content_length(_HugeClResp())

        # 3. {host}{ext}-{index} preserves -{index} instead of replacing suffix with .png
        p = {"url": "https://example.com", "output": "png"}
        r1 = capture_cli.render_output_template("{host}{ext}-{index}", payload=p, index=1)
        r2 = capture_cli.render_output_template("{host}{ext}-{index}", payload=p, index=2)
        self.assertNotEqual(r1, r2)
        self.assertTrue(r1.endswith("-1"), r1)
        self.assertTrue(r2.endswith("-2"), r2)

        # 4. Wide formatted {index:0100d} stays distinct and <= 80-char stem
        p_long = {"url": "https://" + ("a" * 90) + ".com/x", "output": "png"}
        w1 = capture_cli.render_output_template("{host}-{index:0100d}{ext}", payload=p_long, index=1)
        w2 = capture_cli.render_output_template("{host}-{index:0100d}{ext}", payload=p_long, index=2)
        self.assertNotEqual(w1, w2)
        self.assertLessEqual(len(Path(w1).stem), 80)
        self.assertLessEqual(len(Path(w2).stem), 80)

        # 5. Huge integer in config.toml float key raises ValueError instead of OverflowError
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.toml"
            cfg_path.write_text(f"request_timeout = {'9' * 4000}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                cli_config.load_cli_config(cfg_path)

    def test_round13_preset_validation_int_overflow_header_crlf_index_ctrl_and_padded_cl(self) -> None:
        from vipercapture import capture_cli, cli_config

        # 1. preset save validates render options (rejects video-duration-ms=0) and handles overflow safely
        code, _out, err = self._run([
            "preset", "save", "bad-video",
            "--format", "webm",
            "--video-duration-ms", "0",
            "--server", self.server,
            "--no-start",
        ])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(err.strip())["ok"])
        with self.assertRaises(ValueError):
            capture_cli.payload_to_preset_settings({"video": {"duration_ms": 10**1000}})

        # 2. Huge integer in config.toml _INT_KEYS and CLI --retry-delay-ms / --poll-ms exit 2 cleanly
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.toml"
            cfg_path.write_text(f"retry_delay_ms = {'9' * 4000}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                cli_config.load_cli_config(cfg_path)

        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            code_rd = capture_cli.main([
                "https://example.org",
                "--retry-delay-ms", "1" + ("0" * 1000),
                "--no-config",
                "--no-start",
            ])
        self.assertEqual(code_rd, 2)
        self.assertFalse(json.loads(buf_err.getvalue().strip())["ok"])

        code_pm, _out_pm, err_pm = self._run([
            "job", "status", "job-1",
            "--poll-ms", "1" + ("0" * 1000),
            "--server", self.server,
            "--no-start",
        ])
        self.assertEqual(code_pm, 2)
        self.assertFalse(json.loads(err_pm.strip())["ok"])

        # 3. CR/LF in --request-id or --idempotency-key exits 2 cleanly (never raw putheader ValueError)
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            code_hdr = capture_cli.main([
                "https://example.org",
                "--request-id", "abc\r\nX-Injected: 1",
                "--server", self.server,
                "--no-config",
                "--no-start",
            ])
        self.assertEqual(code_hdr, 2)
        self.assertFalse(json.loads(buf_err.getvalue().strip())["ok"])

        # 4. Control-char formatted {index:c} falls back to distinct canonical indexes
        p = {"url": "https://example.com", "output": "png"}
        c1 = capture_cli.render_output_template("{host}-{index:c}{ext}", payload=p, index=1)
        c2 = capture_cli.render_output_template("{host}-{index:c}{ext}", payload=p, index=2)
        self.assertNotEqual(c1, c2)

        # 5. Valid zero-padded Content-Length accepted; >20 significant digits rejected
        class _PaddedCl:
            status = 200
            headers = {"Content-Length": "00000000000000000000000008"}

        self.assertEqual(capture_cli._expected_content_length(_PaddedCl()), 8)

    def test_round14_stdlib_fallback_validation_index_sep_chars_and_crlf_api_key(self) -> None:
        from vipercapture import capture_cli

        # 1. Stdlib fallback validation (without pydantic) rejects invalid video.duration_ms and quality on png
        with self.assertRaises(ValueError):
            capture_cli._validate_payload_fallback({
                "url": "https://example.invalid",
                "output": "webm",
                "video": {"duration_ms": 0},
            })
        with self.assertRaises(ValueError):
            capture_cli._validate_payload_fallback({
                "url": "https://example.invalid",
                "output": "png",
                "image": {"quality": 90},
            })
        with self.assertRaises(ValueError):
            capture_cli._validate_payload_fallback({
                "url": "https://example.invalid",
                "output": "png",
                "headers": {"X-Bad": "a\r\nb"},
            })

        # 2. {index:c} at index 47 ('/') and 92 ('\') canonicalizes to decimal instead of raising ValueError
        p = {"url": "https://example.com", "output": "png"}
        r47 = capture_cli.render_output_template("{host}-{index:c}{ext}", payload=p, index=47)
        r92 = capture_cli.render_output_template("{host}-{index:c}{ext}", payload=p, index=92)
        self.assertEqual(r47, "example.com-47.png")
        self.assertEqual(r92, "example.com-92.png")
        with self.assertRaises(ValueError):
            capture_cli.render_output_template("sub/{host}-{index}{ext}", payload=p, index=1)

        # 3. CR/LF/NUL in --api-key is rejected with exit 2 instead of silently dropped
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            code = capture_cli.main([
                "https://example.org",
                "--api-key", "abc\r\nX-Injected: 1",
                "--server", self.server,
                "--no-config",
                "--no-start",
            ])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(buf_err.getvalue().strip())["ok"])

        code_cmd, _out_cmd, err_cmd = self._run([
            "job", "status", "job-1",
            "--api-key", "abc\r\nX-Injected: 1",
            "--server", self.server,
            "--no-start",
        ])
        self.assertEqual(code_cmd, 2)
        self.assertFalse(json.loads(err_cmd.strip())["ok"])

        # 4. Stdlib fallback matches Pydantic bool-string coercion, image.width/format rules, and wait_for caps
        capture_cli._validate_payload_fallback({
            "url": "https://example.invalid",
            "output": "webm",
            "video": {"scroll": "false", "transparent_background": "0"},
        })
        for bad_payload in (
            {"url": "https://example.invalid", "output": "jpeg", "image": {"optimize_for_speed": "yes"}},
            {"url": "https://example.invalid", "output": "jpeg", "image": {"transparent_background": "yes"}},
            {"url": "https://example.invalid", "output": "webm", "image": {"width": 640}},
            {"url": "https://example.invalid", "output": "png", "wait_for": {"delay_ms": 15001}},
            {"url": "https://example.invalid", "output": "png", "wait_for": {"timeout_ms": 30001}},
        ):
            with self.subTest(payload=bad_payload), self.assertRaises(ValueError):
                capture_cli._validate_payload_fallback(bad_payload)

        # 5. Exact Pydantic v2 parity for bool/int coercion, cross-field rules, and single-page PDF page_ranges
        for ok_payload in (
            {"url": "https://example.invalid", "output": "jpeg", "image": {"transparent_background": 0.0}},
            {"url": "https://example.invalid", "output": "png", "full_page": 1.0},
            {"url": "https://example.invalid", "output": "png", "full_page": b"true"},
            {"url": "https://example.invalid", "output": "png", "wait_for": {"delay_ms": True, "timeout_ms": True}},
            {"url": "https://example.invalid", "output": "png", "wait_for": {"delay_ms": 15000.0}},
        ):
            with self.subTest(ok_payload=ok_payload):
                capture_cli._validate_payload_fallback(ok_payload)

        for parity_bad in (
            {"url": "https://example.invalid", "output": "png", "full_page": " false "},
            {"url": "https://example.invalid", "output": "webm", "cache": "yes"},
            {"url": "https://example.invalid", "output": "webm", "cache": 1},
            {"url": "https://example.invalid", "output": "png", "cache": "false", "cache_key": "abc"},
            {"url": "https://example.invalid", "output": "png", "preserve_viewport_width": "true", "full_page": "false"},
            {"url": "https://example.invalid", "output": "webm", "preserve_viewport_width": 1},
            {"url": "https://example.invalid", "output": "png", "save_profile": 1},
            {"url": "https://example.invalid", "output": "png", "include_shadow_dom": "yes"},
            {"url": "https://example.invalid", "output": "png", "full_page": "true", "selector": "h1"},
            {"url": "https://example.invalid", "output": "png", "full_page": "false", "slices": {"height": 200}},
            {"url": "https://example.invalid", "output": "png", "proceed_on_captcha": "yes", "captcha": {"action": "external"}},
            {"url": "https://example.invalid", "output": "png", "cache": "true", "profile_id": "abc"},
            {"url": "https://example.invalid", "output": "pdf", "pdf": {"mode": "single_page", "page_ranges": "1-2"}},
            {"url": "https://example.invalid", "output": "png", "wait_for": {"delay_ms": 15001.0}},
        ):
            with self.subTest(parity_bad=parity_bad), self.assertRaises(ValueError):
                capture_cli._validate_payload_fallback(parity_bad)

        # 6. Round 17 differential parity: decimal/underscore/bytes/padded ints, floats, artifact bundles, diagnostics, slices, extract_mode, page_ranges
        for ok_r17 in (
            {"url": "https://example.invalid", "output": "webm", "video": {"duration_ms": "5000.0"}},
            {"url": "https://example.invalid", "output": "webm", "video": {"duration_ms": "5_000"}},
            {"url": "https://example.invalid", "output": "webm", "video": {"duration_ms": "5_000.0"}},
            {"url": "https://example.invalid", "output": "webm", "video": {"duration_ms": "0" * 30 + "5000"}},
            {"url": "https://example.invalid", "output": "png", "wait_for": {"delay_ms": "0" * 33}},
            {"url": "https://example.invalid", "output": "png", "wait_for": {"delay_ms": b" 10 "}},
            {"url": "https://example.invalid", "output": "png", "wait_for": {"delay_ms": b"1.0"}},
            {"url": "https://example.invalid", "output": "png", "viewport": {"device_scale_factor": True}},
            {"url": "https://example.invalid", "output": "png", "viewport": {"device_scale_factor": "1.5"}},
        ):
            with self.subTest(ok_r17=ok_r17):
                capture_cli._validate_payload_fallback(ok_r17)

        for bad_r17 in (
            {"url": "https://example.invalid", "output": "png", "wait_for": {"delay_ms": "1.5"}},
            {"url": "https://example.invalid", "output": "png", "wait_for": {"delay_ms": "1e3"}},
            {"url": "https://example.invalid", "output": "pdf", "pdf": {"mode": "single_page", "page_ranges": ""}},
            {"url": "https://example.invalid", "output": "pdf", "pdf": {"mode": "single_page", "page_ranges": 0}},
            {"url": "https://example.invalid", "output": "pdf", "pdf": {"mode": "single_page", "page_ranges": False}},
            {"url": "https://example.invalid", "output": "png", "cache": True, "side_outputs": ["metadata"]},
            {"url": "https://example.invalid", "output": "png", "cache": True, "image": {"thumbnails": [{"name": "small", "width": 100}]}},
            {"url": "https://example.invalid", "output": "png", "full_page": False, "diagnostics": {"bundle": True}, "viewports": [{"name": "a", "width": 800, "height": 600}, {"name": "b", "width": 1024, "height": 768}]},
            {"url": "https://example.invalid", "output": "webm", "diagnostics": {"bundle": True, "include_trace": True}},
            {"url": "https://example.invalid", "output": "png", "full_page": False, "viewports": [{"name": "a", "width": 800, "height": 600}, {"name": "a", "width": 1024, "height": 768}]},
            {"url": "https://example.invalid", "output": "png", "slices": {"height": 50}},
            {"url": "https://example.invalid", "output": "png", "slices": {"height": 200, "overlap": 200}},
            {"url": "https://example.invalid", "output": "png", "extract_mode": "article"},
        ):
            with self.subTest(bad_r17=bad_r17), self.assertRaises(ValueError):
                capture_cli._validate_payload_fallback(bad_r17)


if __name__ == "__main__":
    unittest.main()
