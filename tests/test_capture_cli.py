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


if __name__ == "__main__":
    unittest.main()
