"""Unit and HTTP integration tests for ViperCapture CLI subcommands and config."""

from __future__ import annotations

import base64
import io
import json
import os
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
from vipercapture.cli_config import load_cli_config, read_version
from vipercapture.render_engine import RenderArtifact, _certify_artifact, certification_public_key


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
            self._send_json(200, {
                "control_plane": True,
                "presets": True,
                "browser_engines": ["chromium", "firefox", "webkit"],
                "output_formats": ["png", "jpeg", "pdf"],
                "async_jobs": {"enabled": True, "workers": 4, "queue_limit": 200},
                "gpu": {"mode": state["gpu_mode"], "hardware_active": state["gpu_mode"] == "auto", "mutable": True},
            })
            return
        if path == "/local/presets":
            self._send_json(200, {"presets": state["presets"]})
            return
        if path == "/v1/baselines":
            self._send_json(200, list(state["baselines"].values()))
            return
        if path.startswith("/v1/jobs/") and path.endswith("/result"):
            job_id = path.split("/")[3]
            self._send_bytes(
                200,
                f"ARTIFACT-{job_id}".encode("utf-8"),
                "image/png",
                headers={"Content-Disposition": f'attachment; filename="{job_id}.png"'},
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
            "cert_public_key": certification_public_key(self.cert_secret),
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
            "--color-scheme", "dark",
            "--header", "Authorization: Bearer super-secret-hdr",
            "--server", self.server,
            "--json",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("super-secret-hdr", stdout)
        self.assertEqual(json.loads(stdout)["settings"]["headers"]["Authorization"], "***")

        code, stdout, stderr = self._run(["preset", "list", "--server", self.server])
        self.assertEqual(code, 0, stderr)
        self.assertIn("dark-desktop", stdout)

        code, stdout, stderr = self._run(["preset", "show", "dark-desktop", "--server", self.server, "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("super-secret-hdr", stdout)
        self.assertEqual(json.loads(stdout)["settings"]["viewport"]["width"], 1440)

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
        except ImportError:
            self.skipTest("cryptography not installed")

        artifact = RenderArtifact(b"\x89PNG\r\n\x1a\ncertified-image", "image/png", "capture.png")
        bundle_bytes = _certify_artifact(artifact, self.cert_secret)
        pub_b64 = certification_public_key(self.cert_secret)

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


if __name__ == "__main__":
    unittest.main()
