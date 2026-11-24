"""Cross-platform tests for the vipercapture capture command."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import launch
from vipercapture.capture_cli import (
    EXAMPLE,
    CaptureError,
    _is_descendant_process,
    _read_json,
    _wait_for_local_server,
    api_root,
    build_payload,
    display_path,
    display_server,
    is_local_default,
    main,
    normalize_url,
    parse_capture_args,
    prepare_private_data_dir,
    public_payload,
    resolve_api_url,
    resolve_output,
    safe_filename,
    validate_payload,
)

EXAMPLE_ARGV = [
    "https://dimaths.ma",
    "--output",
    "dimaths.png",
    "--viewport",
    "1440",
    "900",
    "--scale",
    "2",
    "--full-page=false",
    "--color-scheme",
    "dark",
    "--wait",
    "h1",
    "--delay-ms",
    "2500",
    "--click",
    "[aria-label='Activer le thème sombre']",
]


def _payload(argv: list[str]) -> dict:
    return build_payload(parse_capture_args(argv))


class PayloadTests(unittest.TestCase):
    def test_dimaths_command_maps_onto_the_render_contract(self) -> None:
        payload = _payload(EXAMPLE_ARGV)
        self.assertEqual(
            payload,
            {
                "url": "https://dimaths.ma",
                "output": "png",
                "viewport": {"width": 1440, "height": 900, "device_scale_factor": 2.0},
                "full_page": False,
                "environment": {"color_scheme": "dark"},
                "wait_for": {"selector": "h1", "delay_ms": 2500},
                "actions": [{
                    "type": "click",
                    "selector": "[aria-label='Activer le thème sombre']",
                }],
            },
        )

    def test_boolean_forms_match_on_every_shell(self) -> None:
        self.assertFalse(_payload(["https://example.com", "--full-page=false"])["full_page"])
        self.assertFalse(_payload(["https://example.com", "--full-page", "false"])["full_page"])
        self.assertFalse(_payload(["https://example.com", "--no-full-page"])["full_page"])
        self.assertNotIn("full_page", _payload(["https://example.com", "--full-page=true"]))
        self.assertNotIn("full_page", _payload(["https://example.com", "--full-page"]))
        self.assertFalse(
            _payload(["https://example.com", "--no-javascript"])["network"]["java_script_enabled"]
        )
        self.assertFalse(_payload(["https://example.com", "--javascript=false"])["network"]["java_script_enabled"])

    def test_actions_keep_command_order_and_later_timeouts(self) -> None:
        payload = _payload([
            "https://example.com",
            "--action-delay-ms", "10",
            "--click", "a",
            "--fill", "#email", "person@example.com",
            "--hover", "b",
            "--press", "Enter",
            "--press-on", "#field", "Tab",
            "--select", "#plan", "starter", "pro",
            "--hide", ".cookie",
            "--scroll", "0", "400",
            "--scroll-to", "#next",
            "--action-timeout-ms", "8000",
            "--js", "return 1",
            "--action-wait", "#done",
            "--pause-ms", "50",
            "--action", '{"type":"click","selector":"z","delay_ms":5}',
        ])
        self.assertEqual(
            [item["type"] for item in payload["actions"]],
            ["click", "fill", "hover", "press", "press", "select", "hide", "scroll", "scroll", "javascript", "wait", "wait", "click"],
        )
        self.assertEqual(payload["actions"][0]["delay_ms"], 10)
        self.assertNotIn("timeout_ms", payload["actions"][0])
        self.assertEqual(payload["actions"][1]["value"], "person@example.com")
        self.assertEqual(payload["actions"][4], {"type": "press", "selector": "#field", "key": "Tab", "delay_ms": 10})
        self.assertEqual(payload["actions"][5]["values"], ["starter", "pro"])
        self.assertEqual(payload["actions"][7], {"type": "scroll", "x": 0.0, "y": 400.0, "delay_ms": 10})
        self.assertEqual(payload["actions"][9]["timeout_ms"], 8000)
        self.assertEqual(payload["actions"][11], {"type": "wait", "delay_ms": 50, "timeout_ms": 8000})
        self.assertEqual(payload["actions"][12]["delay_ms"], 5)
        self.assertEqual(payload["actions"][12]["timeout_ms"], 8000)

    def test_format_comes_from_the_file_or_the_explicit_flag(self) -> None:
        self.assertEqual(_payload(["https://example.com", "--output", "shot.jpg"])["output"], "jpeg")
        self.assertEqual(_payload(["https://example.com", "--output", "notes.md"])["output"], "markdown")
        self.assertEqual(
            _payload(["https://example.com", "--format", "pdf", "--output", "shot.png"])["output"],
            "pdf",
        )
        self.assertEqual(_payload(["https://example.com", "--format", "jpg"])["output"], "jpeg")
        self.assertEqual(_payload(["dimaths.ma"])["url"], "https://dimaths.ma")

    def test_headers_cookies_proxy_and_blocks(self) -> None:
        with mock.patch.dict(os.environ, {"VIPERCAPTURE_PROXY_PASSWORD": "from-env"}, clear=False):
            payload = _payload([
                "https://example.com",
                "--header", "Accept-Language: fr-FR",
                "--header", "X-Token=abc:def",
                "--cookie", "sid=abc; domain=example.com; path=/; Secure; HttpOnly; SameSite=Lax",
                "--block-url", "*tracker*",
                "--block-resource", "image",
                "--geolocation", "33.57", "-7.58",
                "--geo-accuracy", "20",
                "--proxy", "socks5://proxy.example:1080",
                "--proxy-username", "account",
                "--proxy-bypass", ".internal",
                "--no-javascript",
                "--bypass-csp",
            ])
        self.assertEqual(payload["headers"], {"Accept-Language": "fr-FR", "X-Token": "abc:def"})
        self.assertEqual(payload["network"]["cookies"], [{
            "name": "sid",
            "value": "abc",
            "domain": "example.com",
            "path": "/",
            "secure": True,
            "http_only": True,
            "same_site": "Lax",
        }])
        self.assertEqual(payload["network"]["proxy"]["password"], "from-env")
        self.assertNotIn("network", _payload(["https://example.com"]))
        with mock.patch.dict(os.environ, {"VIPERCAPTURE_PROXY_PASSWORD": ""}, clear=False):
            proxied = _payload(["https://example.com", "--proxy", "http://proxy.example:8080"])
        self.assertNotIn("password", proxied["network"]["proxy"])

    def test_proxy_password_flag_is_not_taken_from_the_environment_without_a_proxy(self) -> None:
        with mock.patch.dict(os.environ, {"VIPERCAPTURE_PROXY_PASSWORD": "secret"}, clear=False):
            payload = _payload(["https://example.com"])
        self.assertNotIn("network", payload)

    def test_nested_feature_flags(self) -> None:
        payload = _payload([
            "https://example.com",
            "--full-page=false",
            "--format", "png",
            "--device", "iphone_14",
            "--locale", "fr-FR",
            "--timezone", "Africa/Casablanca",
            "--media", "screen",
            "--reduced-motion", "reduce",
            "--quality", "80",
            "--format", "webp",
            "--image-width", "640",
            "--transparent",
            "--no-optimize-for-speed",
            "--thumbnail", "card:640",
            "--thumbnail", "preview:320x180",
            "--side", "markdown",
            "--element", "h1",
            "--consent", "reject",
            "--block-ads",
            "--assert-includes", "Hello",
            "--fail-on", "404",
            "--named-viewport", "desktop:1440x900",
            "--named-viewport", "phone:390x844@iphone_14@2",
            "--diagnostics",
            "--har",
            "--no-console",
            "--deterministic",
            "--seed", "7",
            "--timestamp-ms", "1700000000000",
            "--certify",
            "--extract", "article",
            "--shadow-dom",
            "--lazy-load", "thorough",
            "--profile", "accurate",
            "--cache",
            "--cache-key", "home",
            "--engine", "firefox",
            "--user-agent", "ViperCapture",
            "--captcha", "external",
            "--captcha-solver", "desk",
            "--webhook", "https://hooks.example/vipercapture",
            "--pdf-mode", "print",
        ])
        self.assertEqual(payload["output"], "webp")
        self.assertEqual(payload["environment"]["timezone"], "Africa/Casablanca")
        self.assertEqual(payload["image"]["thumbnails"][1], {"name": "preview", "width": 320, "height": 180})
        self.assertTrue(payload["image"]["optimize_for_speed"] is False)
        self.assertEqual(payload["viewports"][1]["device_scale_factor"], 2.0)
        self.assertTrue(payload["diagnostics"]["bundle"])
        self.assertTrue(payload["diagnostics"]["include_har"])
        self.assertFalse(payload["diagnostics"]["include_console"])
        self.assertEqual(payload["deterministic"]["random_seed"], 7)
        self.assertEqual(payload["cleanup"], {"consent_mode": "reject", "block_ads": True})
        self.assertEqual(payload["elements"], [{"selector": "h1"}])
        self.assertTrue(payload["cache"])
        self.assertEqual(payload["cache_key"], "home")
        self.assertEqual(payload["captcha"], {"action": "external", "solver": "desk"})

    def test_pdf_video_and_raw_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            html = Path(directory) / "page.html"
            css = Path(directory) / "page.css"
            html.write_text("<p>Bonjour</p>", encoding="utf-8")
            css.write_text("p{color:red}", encoding="utf-8")
            payload = _payload([
                "--html-file", str(html),
                "--base-url", "example.com",
                "--css-file", str(css),
                "--format", "pdf",
                "--paper", "letter",
                "--orientation", "landscape",
                "--pdf-margin", "0.5",
                "--no-pdf-background",
                "--pdf-tagged=false",
                "--page-ranges", "1-2",
            ])
        self.assertEqual(payload["html"], "<p>Bonjour</p>")
        self.assertEqual(payload["base_url"], "https://example.com")
        self.assertEqual(payload["custom_css"], "p{color:red}")
        self.assertEqual(payload["pdf"]["paper_size"], "Letter")
        self.assertEqual(payload["pdf"]["margins"]["top"], 0.5)
        self.assertFalse(payload["pdf"]["print_background"])
        self.assertFalse(payload["pdf"]["tagged"])
        video = _payload([
            "https://example.com",
            "--format", "webm",
            "--full-page=false",
            "--video-duration-ms", "2000",
            "--fps", "30",
            "--bitrate-mbps", "8",
            "--video-scroll",
        ])
        self.assertEqual(video["video"]["duration_ms"], 2000)
        self.assertTrue(video["video"]["scroll"])

    def test_output_names_are_safe_on_windows(self) -> None:
        self.assertEqual(safe_filename("CON"), "_CON")
        self.assertEqual(safe_filename("a:b"), "a_b")
        self.assertEqual(safe_filename("  ... "), "capture")
        args = parse_capture_args(["https://example.com", "--diagnostics"])
        path = resolve_output(args, _payload(["https://example.com", "--diagnostics"]))
        self.assertEqual(path.name, "example.com.zip")
        stdout_args = parse_capture_args(["https://example.com", "--output", "-"])
        self.assertIsNone(resolve_output(stdout_args, _payload(["https://example.com", "--output", "-"])))

    def test_display_path_stays_relative_inside_the_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                path = Path(directory) / "dimaths.png"
                path.write_bytes(b"x")
                self.assertEqual(display_path(path), "dimaths.png")
            finally:
                os.chdir(previous)

    def test_started_server_uses_a_private_data_directory(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows keeps the default data directory")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            checkout = home / "checkout"
            checkout.mkdir()
            env: dict[str, str] = {}
            prepare_private_data_dir(env, home=home)
            data = home / ".vipercapture" / "data"
            self.assertEqual(env["VIPERCAPTURE_DATA_DIR"], str(data))
            self.assertEqual(data.stat().st_mode & 0o777, 0o700)
            self.assertEqual((home / ".vipercapture").stat().st_mode & 0o777, 0o700)
            explicit = {"VIPERCAPTURE_DATA_DIR": str(checkout)}
            prepare_private_data_dir(explicit, home=home)
            self.assertEqual(explicit["VIPERCAPTURE_DATA_DIR"], str(checkout))

    def test_server_url_and_local_detection(self) -> None:
        self.assertEqual(api_root("http://127.0.0.1:8000/v1/render"), "http://127.0.0.1:8000")
        self.assertEqual(api_root("http://[::1]:9000/v1"), "http://[::1]:9000")
        self.assertEqual(api_root("http://user:s3cret@127.0.0.1:8000/v1"), "http://127.0.0.1:8000")
        self.assertTrue(is_local_default(f"http://{launch.HOST}:{launch.PORT}"))
        self.assertTrue(is_local_default("http://localhost:8000"))
        self.assertFalse(is_local_default("http://127.0.0.1:9000"))
        self.assertFalse(is_local_default("https://example.com"))

    def test_usage_errors_are_json_and_exit_2(self) -> None:
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = main([])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(stderr.getvalue())["ok"])
        stderr.seek(0)
        stderr.truncate()
        with mock.patch("sys.stderr", stderr):
            code = main(["https://example.com", "--full-page=maybe"])
        self.assertEqual(code, 2)
        self.assertIn("true or false", json.loads(stderr.getvalue())["error"])
        with mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_capture_args(["--select", "only-one"])

    def test_help_includes_the_example_and_does_not_start_a_server(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            code = main(["--help"])
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn(EXAMPLE, text)
        self.assertIn("--full-page", text)
        self.assertIn("--scale", text)
        self.assertIn("--click", text)

    def test_dry_run_prints_the_request(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            code = main(["--dry-run", *EXAMPLE_ARGV])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), _payload(EXAMPLE_ARGV))

    def test_dry_run_redacts_the_proxy_password(self) -> None:
        argv = [
            "https://example.com",
            "--proxy",
            "http://proxy.example:8080",
            "--proxy-password",
            "s3cret-password",
        ]
        stdout = io.StringIO()
        with (
            mock.patch("sys.stdout", stdout),
            mock.patch.dict(os.environ, {"VIPERCAPTURE_PROXY_PASSWORD": "env-secret"}, clear=False),
        ):
            code = main(["--dry-run", *argv])
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertNotIn("s3cret-password", text)
        self.assertNotIn("env-secret", text)
        self.assertEqual(json.loads(text)["network"]["proxy"]["password"], "***")
        self.assertEqual(build_payload(parse_capture_args(argv))["network"]["proxy"]["password"], "s3cret-password")
        env_argv = ["https://example.com", "--proxy", "http://proxy.example:8080"]
        with mock.patch.dict(os.environ, {"VIPERCAPTURE_PROXY_PASSWORD": "env-secret"}, clear=False):
            printed = public_payload(build_payload(parse_capture_args(env_argv)))
        self.assertEqual(printed["network"]["proxy"]["password"], "***")

    def test_job_urls_stay_on_the_api_host(self) -> None:
        root = "http://127.0.0.1:8000"
        self.assertEqual(resolve_api_url(root, "/v1/jobs/1"), "http://127.0.0.1:8000/v1/jobs/1")
        self.assertEqual(resolve_api_url(root, "v1/jobs/1"), "http://127.0.0.1:8000/v1/jobs/1")
        self.assertEqual(
            resolve_api_url(root, "http://127.0.0.1:8000/v1/jobs/1/result"),
            "http://127.0.0.1:8000/v1/jobs/1/result",
        )
        self.assertEqual(
            resolve_api_url("https://capture.example", "https://capture.example:443/v1/jobs/1"),
            "https://capture.example:443/v1/jobs/1",
        )
        self.assertEqual(display_server("http://user:s3cret@127.0.0.1:8000/v1"), "http://127.0.0.1:8000/v1")
        for bad in (
            "https://evil.example/v1/jobs/1",
            "http://127.0.0.1:9000/v1/jobs/1",
            "http://localhost:8000/v1/jobs/1",
            "http://user:s3cret@127.0.0.1:8000/v1/jobs/1",
            "//evil.example/v1/jobs/1",
            "javascript:alert(1)",
        ):
            with self.assertRaises(CaptureError):
                resolve_api_url(root, bad)

    def test_json_responses_are_size_capped(self) -> None:
        class Bounded:
            def read(self, amount: int = -1) -> bytes:
                blob = b'{"ok": true}' + b"x" * (2 * 1024 * 1024)
                return blob if amount < 0 else blob[:amount]

        with self.assertRaises(CaptureError):
            _read_json(Bounded())

        class Small:
            def read(self, amount: int = -1) -> bytes:
                blob = b'{"ok": true}'
                return blob if amount < 0 else blob[:amount]

        self.assertEqual(_read_json(Small()), {"ok": True})

    def test_contract_rejects_quality_on_png_when_installed(self) -> None:
        try:
            import vipercapture.render_contract  # noqa: F401
        except ImportError:
            self.skipTest("render contract dependencies are not installed")
        payload = _payload(["https://example.com", "--quality", "40"])
        with self.assertRaises(ValueError):
            validate_payload(payload)
        validate_payload(_payload(EXAMPLE_ARGV))

    def test_javascript_and_data_urls_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_url("javascript:alert(1)")
        self.assertEqual(normalize_url("localhost:8080"), "https://localhost:8080")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        self.server.requests.append(("POST", self.path, raw, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        redirect = getattr(self.server, "redirect_to", None)
        if redirect and self.path == "/v1/render":
            self.send_response(302)
            self.send_header("Location", redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.endswith("/v1/jobs"):
            host, port = self.server.server_address[:2]
            origin = f"http://{host}:{port}"
            mode = getattr(self.server, "job_url", "relative")
            if mode == "foreign":
                document = {
                    "id": "job-1",
                    "status": "queued",
                    "status_url": "https://evil.example/steal",
                    "result_url": None,
                }
            elif mode == "absolute":
                document = {
                    "id": "job-1",
                    "status": "succeeded",
                    "status_url": f"{origin}/v1/jobs/job-1",
                    "result_url": f"{origin}/v1/jobs/job-1/result",
                }
            else:
                document = {
                    "id": "job-1",
                    "status": "queued",
                    "status_url": "/v1/jobs/job-1",
                    "result_url": None,
                }
            self._send(202, json.dumps(document).encode("utf-8"), "application/json")
            return
        if b"\"url\":\"https://fail.example\"" in raw:
            body = json.dumps({
                "error": {"code": "invalid_request", "message": "The page was rejected.", "retryable": False}
            }).encode("utf-8")
            self._send(422, body, "application/json")
            return
        self._send(200, b"\x89PNG\r\n\x1a\nfake", "image/png", extra={"X-Request-Id": "req-1", "X-ViperCapture-Width": "1440"})

    def do_GET(self) -> None:
        self.server.requests.append(("GET", self.path, b"", self.headers.get("Authorization")))  # type: ignore[attr-defined]
        if self.path.endswith("/done"):
            self._send(200, b"\x89PNG\r\n\x1a\nfake", "image/png", extra={"X-Request-Id": "req-1"})
            return
        if self.path.endswith("/result"):
            self._send(200, b"GIF89a", "image/gif")
            return
        body = json.dumps({
            "id": "job-1",
            "status": "succeeded",
            "status_url": "/v1/jobs/job-1",
            "result_url": "/v1/jobs/job-1/result",
        }).encode("utf-8")
        self._send(200, body, "application/json")

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.httpd.job_url = "relative"  # type: ignore[attr-defined]
        self.httpd.redirect_to = None  # type: ignore[attr-defined]
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
            mock.patch("vipercapture.capture_cli.is_local_default", return_value=False),
            mock.patch("vipercapture.capture_cli.load_local_env"),
        ):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_capture_writes_the_response_and_prints_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "dimaths.png"
            code, stdout, stderr = self._run([
                "--server", self.server,
                "--no-start",
                *EXAMPLE_ARGV[:1],
                "--output", str(target),
                *EXAMPLE_ARGV[3:],
            ])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(target.read_bytes(), b"\x89PNG\r\n\x1a\nfake")
            self.assertEqual(Path(stdout.strip()).resolve(), target.resolve())
            self.assertEqual(stderr, "")
            method, path, raw, _auth = self.httpd.requests[0]  # type: ignore[attr-defined]
            self.assertEqual((method, path), ("POST", "/v1/render"))
            self.assertEqual(json.loads(raw)["viewport"]["width"], 1440)

    def test_api_errors_are_json_on_stderr(self) -> None:
        code, _stdout, stderr = self._run([
            "--server", self.server,
            "https://fail.example",
            "--output", "ignored.png",
        ])
        self.assertEqual(code, 1)
        document = json.loads(stderr)
        self.assertEqual(document["status"], 422)
        self.assertEqual(document["code"], "invalid_request")
        self.assertEqual(document["error"], "The page was rejected.")

    def test_json_result_includes_response_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "page.png"
            code, stdout, stderr = self._run([
                "--server", self.server,
                "--json",
                "https://example.com",
                "--output", str(target),
            ])
        self.assertEqual(code, 0, stderr)
        document = json.loads(stdout)
        self.assertTrue(document["ok"])
        self.assertEqual(document["bytes"], len(b"\x89PNG\r\n\x1a\nfake"))
        self.assertEqual(document["request_id"], "req-1")
        self.assertEqual(document["media_type"], "image/png")

    def test_async_polls_and_downloads_the_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "clip.gif"
            code, stdout, stderr = self._run([
                "--server", self.server,
                "--async",
                "--idempotency-key", "clip-1",
                "--poll-ms", "1",
                "https://example.com",
                "--format", "gif",
                "--output", str(target),
            ])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(target.read_bytes(), b"GIF89a")
            self.assertTrue(stdout.strip().endswith("clip.gif"))
            paths = [item[1] for item in self.httpd.requests]  # type: ignore[attr-defined]
            self.assertEqual(paths[0], "/v1/jobs")
            self.assertEqual(paths[-1], "/v1/jobs/job-1/result")

    def test_async_accepts_a_result_url_on_the_same_host(self) -> None:
        self.httpd.job_url = "absolute"  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "clip.gif"
            code, stdout, stderr = self._run([
                "--server", self.server,
                "--async",
                "--poll-ms", "1",
                "https://example.com",
                "--format", "gif",
                "--output", str(target),
            ])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(target.read_bytes(), b"GIF89a")
            self.assertTrue(stdout.strip().endswith("clip.gif"))

    def test_async_refuses_a_status_url_on_another_host(self) -> None:
        self.httpd.job_url = "foreign"  # type: ignore[attr-defined]
        code, stdout, stderr = self._run([
            "--server", self.server,
            "--async",
            "--api-key", "secret-token",
            "--poll-ms", "1",
            "https://example.com",
            "--output", "ignored.png",
        ])
        self.assertEqual(code, 1)
        self.assertNotIn("secret-token", stdout)
        self.assertNotIn("secret-token", stderr)
        self.assertIn("different host", stderr)
        self.assertEqual(len(self.httpd.requests), 1)  # type: ignore[attr-defined]

    def test_verbose_hides_server_userinfo(self) -> None:
        host, port = self.httpd.server_address[:2]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "page.png"
            code, stdout, stderr = self._run([
                "--server", f"http://user:s3cret@{host}:{port}",
                "--verbose",
                "--api-key", "secret-token",
                "https://example.com",
                "--output", str(target),
            ])
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("s3cret", stdout)
        self.assertNotIn("s3cret", stderr)
        self.assertNotIn("secret-token", stdout)
        self.assertNotIn("secret-token", stderr)
        self.assertIn(f"http://{host}:{port}/v1/render", stderr)

    def test_same_origin_redirect_keeps_the_token_on_that_host(self) -> None:
        self.httpd.redirect_to = "/v1/done"  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "page.png"
            code, stdout, stderr = self._run([
                "--server", self.server,
                "--api-key", "secret-token",
                "https://example.com",
                "--output", str(target),
            ])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(target.read_bytes(), b"\x89PNG\r\n\x1a\nfake")
            self.assertNotIn("secret-token", stdout)
            self.assertNotIn("secret-token", stderr)
            authorizations = [item[3] for item in self.httpd.requests]  # type: ignore[attr-defined]
            self.assertEqual(authorizations, ["Bearer secret-token", "Bearer secret-token"])

    def test_authorization_does_not_follow_a_cross_host_redirect(self) -> None:
        other = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        other.requests = []  # type: ignore[attr-defined]
        other.job_url = "relative"  # type: ignore[attr-defined]
        other.redirect_to = None  # type: ignore[attr-defined]
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            _host, port = other.server_address[:2]
            self.httpd.redirect_to = f"http://127.0.0.1:{port}/stolen"  # type: ignore[attr-defined]
            code, stdout, stderr = self._run([
                "--server", self.server,
                "--api-key", "secret-token",
                "https://example.com",
                "--output", "ignored.png",
            ])
            self.assertEqual(code, 1)
            self.assertNotIn("secret-token", stdout)
            self.assertNotIn("secret-token", stderr)
            self.assertIn("another host", stderr)
            self.assertEqual(other.requests, [])  # type: ignore[attr-defined]
            self.assertEqual(len(self.httpd.requests), 1)  # type: ignore[attr-defined]
        finally:
            other.shutdown()
            other.server_close()

    def test_api_key_is_sent_and_not_printed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "page.png"
            code, stdout, stderr = self._run([
                "--server", self.server,
                "--api-key", "secret-token",
                "--verbose",
                "https://example.com",
                "--output", str(target),
            ])
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("secret-token", stdout)
        self.assertNotIn("secret-token", stderr)
        self.assertIn(self.server, stderr)
        self.assertEqual(self.httpd.requests[0][3], "Bearer secret-token")  # type: ignore[attr-defined]


class LaunchDispatchTests(unittest.TestCase):
    def test_capture_help_does_not_create_a_venv(self) -> None:
        stdout = io.StringIO()
        previous = launch.INTERACTIVE_FAILURES
        try:
            with (
                mock.patch("sys.stdout", stdout),
                mock.patch.object(launch, "ensure_venv", side_effect=AssertionError("venv")),
                mock.patch.object(sys, "argv", ["launch.py", "capture", "--help"]),
            ):
                with self.assertRaises(SystemExit) as caught:
                    launch.main()
            self.assertEqual(caught.exception.code, 0)
            self.assertIn("dimaths.ma", stdout.getvalue())
        finally:
            launch.INTERACTIVE_FAILURES = previous


class LocalServerStartupTests(unittest.TestCase):
    def test_is_descendant_process(self) -> None:
        self.assertTrue(_is_descendant_process(os.getpid(), os.getpid()))
        self.assertFalse(_is_descendant_process(999999, 999998))

    def test_wait_for_local_server_accepts_child_process(self) -> None:
        proc = mock.Mock(pid=100)
        proc.poll.return_value = None
        log_path = Path("unused.log")
        with (
            mock.patch.object(launch, "port_open", return_value=True),
            mock.patch.object(launch, "_is_vipercapture_endpoint", return_value=200),
            mock.patch("vipercapture.capture_cli._is_descendant_process", return_value=True),
            mock.patch.object(launch, "_record_server_process") as record_mock,
        ):
            ok = _wait_for_local_server(proc, log_path, token="tok")
            self.assertTrue(ok)
            record_mock.assert_called_once_with(200, "tok", launch.PORT)
            proc.terminate.assert_not_called()

    def test_wait_for_local_server_rejects_unrelated_process(self) -> None:
        proc = mock.Mock(pid=100)
        proc.poll.return_value = None
        log_path = Path("unused.log")
        with (
            mock.patch.object(launch, "port_open", return_value=True),
            mock.patch.object(launch, "_is_vipercapture_endpoint", return_value=300),
            mock.patch("vipercapture.capture_cli._is_descendant_process", return_value=False),
            mock.patch.object(launch, "_clear_server_record") as clear_mock,
        ):
            ok = _wait_for_local_server(proc, log_path, token="tok")
            self.assertFalse(ok)
            proc.terminate.assert_called_once()
            clear_mock.assert_called_once_with(100)


class FastValidationTests(unittest.TestCase):
    def test_simple_payload_validates_without_importing_pydantic(self) -> None:
        from vipercapture.capture_cli import _fast_validate_simple_payload

        self.assertTrue(_fast_validate_simple_payload(_payload(EXAMPLE_ARGV)))
        self.assertTrue(
            _fast_validate_simple_payload(_payload(["--html", "<h1>ok</h1>", "--full-page=false"]))
        )
        # Complex or invalid payloads must fall back to full Pydantic validation.
        self.assertFalse(_fast_validate_simple_payload(_payload(["https://example.com", "--quality", "40"])))
        self.assertFalse(_fast_validate_simple_payload(_payload(["https://example.com", "--viewport", "0", "100"])))
        self.assertFalse(_fast_validate_simple_payload(_payload(["https://example.com", "--timezone", "UTC"])))
        self.assertFalse(_fast_validate_simple_payload({"url": "https://bad..host", "output": "png"}))
        self.assertFalse(_fast_validate_simple_payload({"url": "http://999.999.999.999", "output": "png"}))
        self.assertFalse(_fast_validate_simple_payload({"url": "http://example.123", "output": "png"}))
        self.assertFalse(_fast_validate_simple_payload({"url": "https://example.com", "output": "png", "cache_key": "k"}))
        huge_int = 10**400
        huge_scroll = {"url": "https://example.com", "output": "png", "actions": [{"type": "scroll", "x": huge_int}]}
        self.assertFalse(_fast_validate_simple_payload(huge_scroll))
        self.assertFalse(
            _fast_validate_simple_payload(
                {"url": "https://example.com", "output": "png", "viewport": {"device_scale_factor": huge_int}}
            )
        )
        try:
            import pydantic  # noqa: F401
        except ImportError:
            pydantic_available = False
        else:
            pydantic_available = True
        if pydantic_available:
            with self.assertRaises(ValueError):
                validate_payload({"url": "http://999.999.999.999", "output": "png"})
            with self.assertRaises(ValueError):
                validate_payload({"url": "http://example.123", "output": "png"})
            with self.assertRaises(ValueError):
                validate_payload(huge_scroll)

    def test_relative_redirect_resolves_against_request_path(self) -> None:
        from vipercapture.capture_cli import open_response

        seen_paths: list[str] = []
        redirect_target = ["jobs2"]

        class _RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                seen_paths.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(302)
                self.send_header("Location", redirect_target[0])
                self.end_headers()

            def do_GET(self) -> None:
                seen_paths.append(self.path)
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            resp = open_response(
                f"http://127.0.0.1:{port}/api/v1/jobs",
                method="POST",
                payload={"url": "https://example.com"},
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
            try:
                self.assertEqual(resp.read(), b"ok")
            finally:
                resp.close()

            redirect_target[0] = "//["
            with self.assertRaises(CaptureError):
                open_response(
                    f"http://127.0.0.1:{port}/api/v1/jobs",
                    method="POST",
                    payload={"url": "https://example.com"},
                    headers={"Content-Type": "application/json"},
                    timeout=5,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(seen_paths, ["/api/v1/jobs", "/api/v1/jobs2", "/api/v1/jobs"])

    def test_local_http_rejects_non_redirect_3xx(self) -> None:
        from vipercapture.capture_cli import open_response

        class _NotModifiedHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(304)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _NotModifiedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with self.assertRaises(CaptureError) as ctx:
                open_response(
                    f"http://127.0.0.1:{port}/v1/render",
                    method="GET",
                    payload=None,
                    headers={},
                    timeout=5,
                )
            self.assertEqual(ctx.exception.status, 304)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_ensure_server_checks_port_open_before_http_probe(self) -> None:
        from vipercapture.capture_cli import ensure_server

        args = parse_capture_args(["https://example.com", "--no-start"])
        with (
            mock.patch.object(launch, "port_open", return_value=False) as port_open,
            mock.patch.object(launch, "_is_vipercapture_endpoint", side_effect=AssertionError("should not probe")),
        ):
            with self.assertRaises(CaptureError):
                ensure_server(args)
        port_open.assert_called_once()


class NewCaptureFeaturesTests(unittest.TestCase):
    def test_action_wait_text_and_js_file_preserve_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "script.js"
            script.write_text("document.title = 'updated';", encoding="utf-8")
            payload = _payload([
                "https://example.com",
                "--action-delay-ms", "25",
                "--action-wait-text", "Ready to go",
                "--js-file", str(script),
                "--click", "#go",
            ])
        self.assertEqual(
            payload["actions"],
            [
                {"type": "wait", "value": "Ready to go", "delay_ms": 25},
                {"type": "javascript", "value": "document.title = 'updated';", "delay_ms": 25},
                {"type": "click", "selector": "#go", "delay_ms": 25},
            ],
        )

    def test_cookie_file_formats_and_secret_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            netscape = Path(directory) / "cookies.txt"
            netscape.write_text(
                "# Netscape HTTP Cookie File\n"
                "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t1893456000\tsid\tSECRET_NS_VAL\n",
                encoding="utf-8",
            )
            payload = _payload(["https://example.com", "--cookie-file", str(netscape)])
            self.assertEqual(payload["network"]["cookies"][0]["name"], "sid")
            self.assertEqual(payload["network"]["cookies"][0]["value"], "SECRET_NS_VAL")
            self.assertTrue(payload["network"]["cookies"][0]["http_only"])
            self.assertTrue(payload["network"]["cookies"][0]["secure"])
            redacted = public_payload(payload)
            self.assertEqual(redacted["network"]["cookies"][0]["value"], "***")

            cookie_json = Path(directory) / "cookies.json"
            cookie_json.write_text(
                json.dumps([{"name": "tok", "value": "SECRET_JSON_VAL", "domain": "example.com", "sameSite": "strict"}]),
                encoding="utf-8",
            )
            payload2 = _payload(["https://example.com", "--cookie-file", str(cookie_json)])
            self.assertEqual(payload2["network"]["cookies"][0]["same_site"], "Strict")

            hdr_file = Path(directory) / "hdr.txt"
            hdr_file.write_text("Cookie: a=1; b=SECRET_HDR", encoding="utf-8")
            payload3 = _payload(["https://example.com", "--cookie-file", str(hdr_file)])
            self.assertEqual(len(payload3["network"]["cookies"]), 2)
            self.assertEqual(payload3["network"]["cookies"][0]["domain"], "example.com")

            # --dry-run must redact cookie values and sensitive headers
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = main([
                    "--dry-run",
                    "--no-config",
                    "https://example.com",
                    "--cookie-file", str(netscape),
                    "--header", "Authorization: Bearer TOP_SECRET_BEARER",
                ])
            self.assertEqual(code, 0)
            out_text = stdout.getvalue()
            self.assertNotIn("SECRET_NS_VAL", out_text)
            self.assertNotIn("TOP_SECRET_BEARER", out_text)

    def test_from_json_and_cli_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base.json"
            base.write_text(
                json.dumps({
                    "url": "https://base.example",
                    "output": "webp",
                    "full_page": False,
                    "viewport": {"width": 1280, "height": 720},
                    "environment": {"color_scheme": "dark", "locale": "fr-FR"},
                }),
                encoding="utf-8",
            )
            payload = _payload([
                "--from-json", str(base),
                "--full-page=true",
                "--scale", "2",
                "--color-scheme", "light",
            ])
        self.assertEqual(payload["url"], "https://base.example")
        self.assertEqual(payload["output"], "webp")
        self.assertTrue(payload["full_page"])
        self.assertEqual(payload["viewport"], {"width": 1280, "height": 720, "device_scale_factor": 2.0})
        self.assertEqual(payload["environment"], {"color_scheme": "light", "locale": "fr-FR"})

    def test_preset_loads_web_ui_preset_from_local_file_with_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            presets_dir = Path(directory) / "presets"
            presets_dir.mkdir()
            (presets_dir / "presets.json").write_text(
                json.dumps([
                    {
                        "name": "mobile-dark",
                        "settings": {
                            "output": "png",
                            "width": 390,
                            "height": 844,
                            "density": 2,
                            "fullPage": False,
                            "colorScheme": "dark",
                            "waitDelay": 2,
                            "blockAds": True,
                        },
                    }
                ]),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"VIPERCAPTURE_DATA_DIR": directory}, clear=False):
                payload = _payload([
                    "https://example.com",
                    "--preset", "mobile-dark",
                    "--format", "jpeg",
                    "--server", "http://127.0.0.1:1",
                ])
        self.assertEqual(payload["url"], "https://example.com")
        self.assertEqual(payload["output"], "jpeg")
        self.assertFalse(payload["full_page"])
        self.assertEqual(payload["viewport"], {"width": 390, "height": 844, "device_scale_factor": 2.0})
        self.assertEqual(payload["environment"]["color_scheme"], "dark")
        self.assertEqual(payload["wait_for"]["delay_ms"], 2000)
        self.assertTrue(payload["cleanup"]["block_ads"])

    def test_config_toml_defaults_and_no_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = Path(directory) / "config.toml"
            cfg.write_text(
                "[capture]\n"
                'format = "webp"\n'
                "viewport = [1600, 900]\n"
                'color_scheme = "dark"\n'
                "full_page = false\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"VIPERCAPTURE_CONFIG": str(cfg)}, clear=False):
                stdout = io.StringIO()
                with mock.patch("sys.stdout", stdout):
                    code = main(["--dry-run", "https://example.com"])
                self.assertEqual(code, 0)
                doc = json.loads(stdout.getvalue())
                self.assertEqual(doc["output"], "webp")
                self.assertEqual(doc["viewport"], {"width": 1600, "height": 900})
                self.assertEqual(doc["environment"]["color_scheme"], "dark")
                self.assertFalse(doc["full_page"])

                # CLI flag overrides config.toml
                stdout = io.StringIO()
                with mock.patch("sys.stdout", stdout):
                    code = main(["--dry-run", "https://example.com", "--format", "png"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout.getvalue())["output"], "png")

                # --no-config ignores config.toml
                stdout = io.StringIO()
                with mock.patch("sys.stdout", stdout):
                    code = main(["--dry-run", "--no-config", "https://example.com"])
                self.assertEqual(code, 0)
                doc_no_cfg = json.loads(stdout.getvalue())
                self.assertEqual(doc_no_cfg["output"], "png")
                self.assertNotIn("viewport", doc_no_cfg)

    def test_async_no_wait_and_retries_and_multi_url_and_stop_server(self) -> None:
        attempts = {"render": 0}

        class _FlakyHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                if self.path == "/v1/jobs":
                    body = json.dumps({"id": "async-job-99", "status": "queued", "status_url": "/v1/jobs/async-job-99"}).encode()
                    self.send_response(202)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                attempts["render"] += 1
                if attempts["render"] == 1:
                    body = b'{"error":{"code":"busy","message":"try again"}}'
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = b"\x89PNG\r\n\x1a\nOK"
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:
                return

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}"
            # --no-wait without --async fails with exit 2
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                self.assertEqual(main(["--no-config", "https://example.com", "--no-wait"]), 2)

            # --async --no-wait prints job id immediately
            stdout = io.StringIO()
            with (
                mock.patch("sys.stdout", stdout),
                mock.patch("vipercapture.capture_cli.is_local_default", return_value=False),
                mock.patch("vipercapture.capture_cli.load_local_env"),
            ):
                code = main(["--no-config", "--server", url, "--async", "--no-wait", "https://example.com"])
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue().strip(), "async-job-99")

            # --retries recovers from 503 and --open / --copy / --stop-server hooks are called
            with tempfile.TemporaryDirectory() as directory:
                out = Path(directory) / "shot.png"
                stdout = io.StringIO()
                with (
                    mock.patch("sys.stdout", stdout),
                    mock.patch("vipercapture.capture_cli.ensure_server", return_value=True),
                    mock.patch("vipercapture.capture_cli.stop_started_server") as stop_mock,
                    mock.patch("vipercapture.capture_cli.open_saved_file") as open_mock,
                    mock.patch("vipercapture.capture_cli.copy_output_to_clipboard", return_value="path") as copy_mock,
                    mock.patch("vipercapture.capture_cli.load_local_env"),
                ):
                    code = main([
                        "--no-config",
                        "--server", url,
                        "--retries", "2",
                        "--retry-delay-ms", "1",
                        "--open",
                        "--copy",
                        "--stop-server",
                        "https://example.com",
                        "--output", str(out),
                    ])
                self.assertEqual(code, 0)
                self.assertEqual(attempts["render"], 2)
                self.assertEqual(out.read_bytes(), b"\x89PNG\r\n\x1a\nOK")
                open_mock.assert_called_once()
                copy_mock.assert_called_once()
                stop_mock.assert_called_once()

                # Multi-URL with --urls-file, --output-dir, --output-template
                urls_file = Path(directory) / "urls.txt"
                urls_file.write_text("https://one.example\nhttps://two.example\n", encoding="utf-8")
                out_dir = Path(directory) / "batch"
                stdout = io.StringIO()
                with (
                    mock.patch("sys.stdout", stdout),
                    mock.patch("vipercapture.capture_cli.is_local_default", return_value=False),
                    mock.patch("vipercapture.capture_cli.load_local_env"),
                ):
                    code = main([
                        "--no-config",
                        "--server", url,
                        "--urls-file", str(urls_file),
                        "--output-dir", str(out_dir),
                        "--output-template", "{index}-{host}{ext}",
                        "--json",
                    ])
                self.assertEqual(code, 0)
                summary = json.loads(stdout.getvalue())
                self.assertTrue(summary["ok"])
                self.assertEqual(summary["succeeded"], 2)
                self.assertTrue((out_dir / "1-one.example.png").is_file())
                self.assertTrue((out_dir / "2-two.example.png").is_file())
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
