import asyncio
import io
import json
import socket
import sys
import threading
import unittest
import zipfile
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from pydantic import ValidationError
from starlette.requests import Request

from vipercapture.main import (
    STEALTH,
    _launch_browser,
    _await_while_connected,
    _is_local_control_request,
    gpu_launch_args,
    hardware_gpu_active,
)
from vipercapture.render_contract import (
    BrowserEngine,
    LazyLoadMode,
    OutputFormat,
    RenderRequest,
)
from vipercapture.render_engine import (
    PublicUrlValidator,
    RenderEngine,
    RenderLimits,
    capture_cdp_image,
    ensure_dimensions,
    ensure_full_page_dimensions,
    is_public_http_url,
    load_lazy_content,
    needs_request_routing,
    routed_headers,
)
from vipercapture.render_errors import RenderError


class _RenderFixtureHandler(BaseHTTPRequestHandler):
    PAGES = {
        "/content": """<!doctype html><style>
            html,body{margin:0;background:#fff}
            #target{width:120px;height:80px;background:#e11d48;color:#fff}
        </style><div id="target">ViperCapture</div>""",
        "/blank": """<!doctype html><style>
            html,body{margin:0;width:100%;height:100%;background:#fff}
        </style>""",
        "/tall": """<!doctype html><style>
            html,body{margin:0}
            body{height:1800px;background:linear-gradient(#2563eb,#e11d48)}
        </style>""",
        "/transparent": """<!doctype html><style>
            html,body{margin:0;background:transparent}
            #target{width:20px;height:20px;background:#e11d48}
        </style><div id="target"></div>""",
        "/scrolled": """<!doctype html><style>
            html,body{margin:0}
            body{height:800px;background:linear-gradient(
                to bottom,
                #e11d48 0 300px,
                #2563eb 300px 600px,
                #16a34a 600px
            )}
            #target{position:absolute;top:420px;left:20px;width:80px;height:60px;
                background:#facc15}
        </style><div id="target"></div><script>scrollTo(0, 300)</script>""",
        "/hostile-animation-frame": """<!doctype html><style>
            html,body{margin:0;width:100%;height:100%;background:#2563eb}
        </style><script>window.requestAnimationFrame = () => 1</script>""",
    }

    def do_GET(self) -> None:
        body = self.PAGES.get(self.path)
        if body is None:
            self.send_error(404)
            return
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class SsrfTests(unittest.IsolatedAsyncioTestCase):
    @patch("vipercapture.render_engine.socket.getaddrinfo")
    async def test_private_address_is_blocked(self, getaddrinfo):
        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]
        self.assertFalse(await is_public_http_url("https://example.com"))

    @patch("vipercapture.render_engine.socket.getaddrinfo")
    async def test_public_address_is_allowed(self, getaddrinfo):
        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        self.assertTrue(await is_public_http_url("https://example.com"))

    @patch("vipercapture.render_engine.socket.getaddrinfo")
    async def test_mixed_dns_result_is_blocked(self, getaddrinfo):
        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]
        self.assertFalse(await is_public_http_url("https://example.com"))

    @patch("vipercapture.render_engine._resolve_public_origin", new_callable=AsyncMock)
    async def test_render_validator_coalesces_only_inflight_checks(self, resolve):
        started = asyncio.Event()
        release = asyncio.Event()

        async def resolve_public(_hostname, _port):
            started.set()
            await release.wait()
            return True

        resolve.side_effect = resolve_public
        validator = PublicUrlValidator()
        first = asyncio.create_task(
            validator.is_public("https://example.com/one")
        )
        await started.wait()
        second = asyncio.create_task(
            validator.is_public("https://example.com/two")
        )
        await asyncio.sleep(0)

        self.assertEqual(resolve.await_count, 1)
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [True, True])

    @patch("vipercapture.render_engine.socket.getaddrinfo")
    async def test_render_validator_rechecks_sequential_requests(self, getaddrinfo):
        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        validator = PublicUrlValidator()

        self.assertTrue(await validator.is_public("https://example.com/one"))
        self.assertTrue(await validator.is_public("https://example.com/two"))
        self.assertEqual(getaddrinfo.call_count, 2)


class HeaderRoutingTests(unittest.TestCase):
    def test_custom_headers_reach_same_origin(self):
        result = routed_headers(
            "https://example.com/page",
            "https://example.com",
            {"Accept": "text/html", "Authorization": "browser"},
            {"Authorization": "custom"},
        )
        self.assertEqual(result["Authorization"], "custom")
        self.assertEqual(result["Accept"], "text/html")

    def test_custom_headers_do_not_follow_cross_origin(self):
        result = routed_headers(
            "https://cdn.example.net/asset",
            "https://example.com",
            {"Accept": "image/*", "Authorization": "browser"},
            {"Authorization": "custom"},
        )
        self.assertNotIn("Authorization", result)
        self.assertEqual(result["Accept"], "image/*")

    def test_local_request_without_headers_skips_routing(self):
        self.assertFalse(needs_request_routing(False, {}))

    def test_hosted_and_custom_header_requests_keep_routing(self):
        self.assertTrue(needs_request_routing(True, {}))
        self.assertTrue(
            needs_request_routing(False, {"Authorization": "Bearer test"})
        )


class CaptureOptimizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_lazy_loading_does_not_touch_page(self):
        page = AsyncMock()

        await load_lazy_content(page, 720, LazyLoadMode.NONE)

        page.evaluate.assert_not_awaited()

    async def test_adaptive_lazy_loading_uses_shorter_settle_delay(self):
        page = AsyncMock()

        async def evaluate(script, *_args):
            return 720 if script.startswith("Math.max") else None

        page.evaluate.side_effect = evaluate
        with patch("vipercapture.render_engine.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await load_lazy_content(page, 720, LazyLoadMode.ADAPTIVE)

        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.075, 0.2])

    async def test_thorough_lazy_loading_preserves_existing_delay(self):
        page = AsyncMock()

        async def evaluate(script, *_args):
            return 720 if script.startswith("Math.max") else None

        page.evaluate.side_effect = evaluate
        with patch("vipercapture.render_engine.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await load_lazy_content(page, 720, LazyLoadMode.THOROUGH)

        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.2, 0.2])

    async def test_fast_png_sets_cdp_speed_flag(self):
        session = AsyncMock()

        async def send(method, _params=None):
            return {"data": "eA=="} if method == "Page.captureScreenshot" else {}

        session.send.side_effect = send
        page = AsyncMock()
        page.context.new_cdp_session.return_value = session

        result = await capture_cdp_image(
            page,
            output=OutputFormat.PNG,
            clip={"x": 0, "y": 0, "width": 100, "height": 100, "scale": 1},
            quality=None,
            transparent=False,
            optimize_for_speed=True,
        )

        self.assertEqual(result, b"x")
        capture_call = next(
            call
            for call in session.send.await_args_list
            if call.args[0] == "Page.captureScreenshot"
        )
        self.assertTrue(capture_call.args[1]["optimizeForSpeed"])
        self.assertEqual(capture_call.args[1]["format"], "png")
        self.assertNotIn("quality", capture_call.args[1])
        evaluated = " ".join(
            str(call.args[0]) for call in page.evaluate.await_args_list
        )
        self.assertNotIn("requestAnimationFrame", evaluated)

    async def test_webp_does_not_claim_fast_encoding(self):
        session = AsyncMock()

        async def send(method, _params=None):
            return {"data": "eA=="} if method == "Page.captureScreenshot" else {}

        session.send.side_effect = send
        page = AsyncMock()
        page.context.new_cdp_session.return_value = session

        result = await capture_cdp_image(
            page,
            output=OutputFormat.WEBP,
            clip={"x": 0, "y": 0, "width": 100, "height": 100, "scale": 1},
            quality=82,
            transparent=False,
            optimize_for_speed=False,
        )

        self.assertEqual(result, b"x")
        capture_call = next(
            call
            for call in session.send.await_args_list
            if call.args[0] == "Page.captureScreenshot"
        )
        self.assertNotIn("optimizeForSpeed", capture_call.args[1])
        self.assertEqual(capture_call.args[1]["quality"], 82)
        evaluated = " ".join(
            str(call.args[0]) for call in page.evaluate.await_args_list
        )
        self.assertNotIn("requestAnimationFrame", evaluated)


class BrowserCaptureRegressionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _RenderFixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    async def _pixel_stats(self, page, image: bytes, media_type: str) -> dict:
        encoded = b64encode(image).decode("ascii")
        return await page.evaluate(
            """async ({ encoded, mediaType }) => {
                const image = new Image();
                image.src = `data:${mediaType};base64,${encoded}`;
                await image.decode();
                const canvas = document.createElement("canvas");
                canvas.width = image.naturalWidth;
                canvas.height = image.naturalHeight;
                const context = canvas.getContext("2d");
                context.drawImage(image, 0, 0);
                const pixels = context.getImageData(
                    0, 0, canvas.width, canvas.height
                ).data;
                const first = pixels.slice(0, 4);
                let different = 0;
                let transparent = 0;
                for (let index = 0; index < pixels.length; index += 4) {
                    if (
                        pixels[index] !== first[0]
                        || pixels[index + 1] !== first[1]
                        || pixels[index + 2] !== first[2]
                        || pixels[index + 3] !== first[3]
                    ) different += 1;
                    if (pixels[index + 3] === 0) transparent += 1;
                }
                return {
                    width: canvas.width,
                    height: canvas.height,
                    first: Array.from(first),
                    different,
                    transparent,
                };
            }""",
            {"encoded": encoded, "mediaType": media_type},
        )

    async def test_deterministic_mode_controls_crypto_and_performance(self):
        source = """<script>
        document.body.textContent = [
          crypto.randomUUID(),
          Array.from(crypto.getRandomValues(new Uint8Array(8))).join(','),
          performance.timeOrigin,
          performance.now()
        ].join('|');
        </script>"""
        request = RenderRequest(
            html=source,
            full_page=False,
            deterministic={"enabled": True, "random_seed": 42},
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                first = await RenderEngine(hosted=False).render_image(
                    browser, request, RenderLimits()
                )
                second = await RenderEngine(hosted=False).render_image(
                    browser, request, RenderLimits()
                )
            finally:
                await browser.close()
        self.assertEqual(first.body, second.body)

    async def test_failed_artifact_does_not_persist_profile_state(self):
        loader = AsyncMock(return_value={"cookies": [], "origins": []})
        saver = AsyncMock()
        request = RenderRequest(
            html="profile",
            full_page=False,
            profile_id="profile",
            save_profile=True,
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                with (
                    patch(
                        "vipercapture.render_engine.diagnostic_bundle",
                        AsyncMock(
                            side_effect=RenderError(
                                "output_too_large", "failed", 413, False
                            )
                        ),
                    ),
                    self.assertRaises(RenderError),
                ):
                    await RenderEngine(
                        hosted=False,
                        profile_loader=loader,
                        profile_saver=saver,
                    ).render_image(browser, request, RenderLimits())
            finally:
                await browser.close()
        saver.assert_not_awaited()

    async def test_diagnostic_console_is_bounded_before_transport(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                artifact = await RenderEngine(hosted=False).render_image(
                    browser,
                    RenderRequest(
                        html="<script>console.log('x'.repeat(1_000_000))</script>",
                        full_page=False,
                        diagnostics={"bundle": True},
                    ),
                    RenderLimits(),
                )
            finally:
                await browser.close()
        with zipfile.ZipFile(io.BytesIO(artifact.body)) as archive:
            messages = json.loads(archive.read("console.json"))
        self.assertEqual(len(messages), 1)
        self.assertLessEqual(len(messages[0]["text"]), 4_096)

    async def test_fast_png_preserves_capture_semantics(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                engine = RenderEngine(hosted=False)
                limits = RenderLimits()
                base = {
                    "output": "png",
                    "full_page": False,
                    "lazy_load": "none",
                    "image": {"optimize_for_speed": True},
                }

                content = await engine.render_image(
                    browser,
                    RenderRequest(url=f"{self.base_url}/content", **base),
                    limits,
                )
                blank = await engine.render_image(
                    browser,
                    RenderRequest(url=f"{self.base_url}/blank", **base),
                    limits,
                )
                selector = await engine.render_image(
                    browser,
                    RenderRequest(
                        url=f"{self.base_url}/content",
                        output="png",
                        full_page=False,
                        lazy_load="none",
                        selector="#target",
                        viewport={
                            "width": 320,
                            "height": 240,
                            "device_scale_factor": 2,
                        },
                        image={"optimize_for_speed": True},
                    ),
                    limits,
                )
                tall = await engine.render_image(
                    browser,
                    RenderRequest(
                        url=f"{self.base_url}/tall",
                        output="png",
                        full_page=True,
                        lazy_load="none",
                        viewport={"width": 320, "height": 240},
                        image={"optimize_for_speed": True},
                    ),
                    limits,
                )
                transparent = await engine.render_image(
                    browser,
                    RenderRequest(
                        url=f"{self.base_url}/transparent",
                        output="png",
                        full_page=False,
                        lazy_load="none",
                        viewport={"width": 160, "height": 120},
                        image={
                            "transparent_background": True,
                            "optimize_for_speed": True,
                        },
                    ),
                    limits,
                )
                scrolled_viewport = await engine.render_image(
                    browser,
                    RenderRequest(
                        url=f"{self.base_url}/scrolled",
                        output="png",
                        full_page=False,
                        lazy_load="none",
                        viewport={"width": 160, "height": 100},
                        image={"optimize_for_speed": True},
                    ),
                    limits,
                )
                scrolled_selector = await engine.render_image(
                    browser,
                    RenderRequest(
                        url=f"{self.base_url}/scrolled",
                        output="png",
                        full_page=False,
                        lazy_load="none",
                        selector="#target",
                        viewport={"width": 160, "height": 100},
                        image={"optimize_for_speed": True},
                    ),
                    limits,
                )
                hostile_animation_frame = await asyncio.wait_for(
                    engine.render_image(
                        browser,
                        RenderRequest(
                            url=f"{self.base_url}/hostile-animation-frame",
                            **base,
                        ),
                        limits,
                    ),
                    timeout=5,
                )
                legacy_webp = await engine.render_image(
                    browser,
                    RenderRequest(
                        url=f"{self.base_url}/content",
                        output="webp",
                        full_page=False,
                        lazy_load="none",
                        image={"optimize_for_speed": True},
                    ),
                    limits,
                )

                decoder = await browser.new_page()
                content_stats = await self._pixel_stats(
                    decoder, content.body, content.media_type
                )
                blank_stats = await self._pixel_stats(
                    decoder, blank.body, blank.media_type
                )
                selector_stats = await self._pixel_stats(
                    decoder, selector.body, selector.media_type
                )
                tall_stats = await self._pixel_stats(
                    decoder, tall.body, tall.media_type
                )
                transparent_stats = await self._pixel_stats(
                    decoder, transparent.body, transparent.media_type
                )
                scrolled_viewport_stats = await self._pixel_stats(
                    decoder, scrolled_viewport.body, scrolled_viewport.media_type
                )
                scrolled_selector_stats = await self._pixel_stats(
                    decoder, scrolled_selector.body, scrolled_selector.media_type
                )
                hostile_animation_frame_stats = await self._pixel_stats(
                    decoder,
                    hostile_animation_frame.body,
                    hostile_animation_frame.media_type,
                )

                self.assertGreater(content_stats["different"], 0)
                self.assertEqual(blank_stats["different"], 0)
                self.assertEqual(
                    (selector_stats["width"], selector_stats["height"]),
                    (240, 160),
                )
                self.assertEqual(
                    (tall_stats["width"], tall_stats["height"]),
                    (320, 1800),
                )
                self.assertGreater(transparent_stats["transparent"], 0)
                self.assertEqual(
                    scrolled_viewport_stats["first"],
                    [37, 99, 235, 255],
                )
                self.assertEqual(
                    scrolled_selector_stats["first"],
                    [250, 204, 21, 255],
                )
                self.assertEqual(
                    hostile_animation_frame_stats["first"],
                    [37, 99, 235, 255],
                )
                self.assertEqual(legacy_webp.media_type, "image/webp")
            finally:
                await browser.close()

    async def test_webm_contains_only_the_post_preparation_window(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                artifact = await RenderEngine(hosted=False).render(
                    browser,
                    RenderRequest.model_validate(
                        {
                            "html": "<h1>Timed recording</h1>",
                            "output": "webm",
                            "wait_for": {"delay_ms": 500},
                            "video": {"duration_ms": 1000},
                        }
                    ),
                    RenderLimits(deadline_seconds=30),
                )
            finally:
                await browser.close()
        self.assertEqual(artifact.body[:4], bytes.fromhex("1a45dfa3"))
        self.assertGreaterEqual(artifact.metadata["duration_ms"], 850)
        self.assertLessEqual(artifact.metadata["duration_ms"], 1_100)


class CaptureCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_work_returns_without_waiting_on_disconnect_poll(self):
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

        result = await asyncio.wait_for(
            _await_while_connected(request, asyncio.sleep(0.01, result="done")),
            timeout=1,
        )

        self.assertEqual(result, "done")

    async def test_disconnect_cancels_in_flight_work(self):
        operation_started = asyncio.Event()
        operation_cancelled = asyncio.Event()

        async def operation():
            operation_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                operation_cancelled.set()

        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
        with self.assertRaises(RenderError) as error:
            await _await_while_connected(request, operation())

        self.assertEqual(error.exception.code, "client_disconnected")
        self.assertEqual(error.exception.status_code, 499)
        self.assertTrue(operation_started.is_set())
        self.assertTrue(operation_cancelled.is_set())


class BrowserLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_chromium_uses_full_headless_browser_and_writable_paths(self):
        browser = object()
        launch = AsyncMock(return_value=browser)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        self.assertIs(
            await _launch_browser(playwright, "off", BrowserEngine.CHROMIUM),
            browser,
        )

        options = launch.await_args.kwargs
        self.assertEqual(options["channel"], "chromium")
        self.assertTrue(options["headless"])
        self.assertEqual(options["args"], [])
        self.assertEqual(options["env"]["XDG_CACHE_HOME"], "/tmp/chromium-cache")
        self.assertEqual(options["env"]["XDG_CONFIG_HOME"], "/tmp/chromium-config")

    async def test_non_chromium_launch_does_not_receive_chromium_options(self):
        browser = object()
        launch = AsyncMock(return_value=browser)
        playwright = SimpleNamespace(firefox=SimpleNamespace(launch=launch))

        self.assertIs(
            await _launch_browser(playwright, "off", BrowserEngine.FIREFOX),
            browser,
        )

        self.assertEqual(launch.await_args.kwargs, {"headless": True, "args": []})


class GpuConfigurationTests(unittest.TestCase):
    def test_gpu_is_off_by_default(self):
        self.assertEqual(gpu_launch_args("off", "default", "linux"), [])

    def test_stealth_preserves_runtime_browser_signals(self):
        expected_platform = (
            "Win32"
            if sys.platform.startswith("win")
            else "MacIntel"
            if sys.platform == "darwin"
            else "Linux x86_64"
        )
        self.assertEqual(STEALTH.navigator_platform_override, expected_platform)
        self.assertFalse(STEALTH.sec_ch_ua)
        self.assertFalse(STEALTH.webgl_vendor)

    def test_linux_vulkan_mode_sets_explicit_backend(self):
        self.assertEqual(
            gpu_launch_args("auto", "vulkan", "linux"),
            ["--enable-gpu", "--use-angle=vulkan"],
        )

    def test_software_renderers_do_not_satisfy_required_gpu(self):
        self.assertFalse(
            hardware_gpu_active(
                {
                    "gpu": {
                        "devices": [{"deviceString": "SwiftShader Device"}],
                        "featureStatus": {"gpu_compositing": "enabled"},
                    }
                }
            )
        )

    def test_hardware_compositing_satisfies_required_gpu(self):
        self.assertTrue(
            hardware_gpu_active(
                {
                    "gpu": {
                        "devices": [{"deviceString": "Example Hardware GPU"}],
                        "featureStatus": {"gpu_compositing": "enabled"},
                    }
                }
            )
        )

    def test_gpu_control_accepts_same_origin_loopback_request(self):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "http",
                "path": "/local/gpu-mode",
                "query_string": b"",
                "server": ("127.0.0.1", 8000),
                "client": ("127.0.0.1", 44000),
                "headers": [
                    (b"host", b"127.0.0.1:8000"),
                    (b"origin", b"http://127.0.0.1:8000"),
                ],
            }
        )

        self.assertTrue(_is_local_control_request(request))

    def test_gpu_control_rejects_remote_or_cross_origin_request(self):
        for client, origin in [
            ("203.0.113.8", "http://127.0.0.1:8000"),
            ("127.0.0.1", "https://attacker.example"),
        ]:
            with self.subTest(client=client, origin=origin):
                request = Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "scheme": "http",
                        "path": "/local/gpu-mode",
                        "query_string": b"",
                        "server": ("127.0.0.1", 8000),
                        "client": (client, 44000),
                        "headers": [
                            (b"host", b"127.0.0.1:8000"),
                            (b"origin", origin.encode()),
                        ],
                    }
                )

                self.assertFalse(_is_local_control_request(request))


class FrontendTests(unittest.TestCase):
    def test_built_frontend_uses_vipercapture_brand_and_assets(self):
        root = Path(__file__).resolve().parent.parent
        index = (root / "static" / "app" / "index.html").read_text(encoding="utf-8")
        source = (root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        desktop_source = (root / "desktop" / "src" / "App.tsx").read_text(
            encoding="utf-8"
        )
        components = (root / "frontend" / "components.json").read_text(encoding="utf-8")

        self.assertIn("ViperCapture | Open-source webpage rendering", index)
        self.assertIn("/static/app/assets/", index)
        self.assertIn("GPU rendering", source)
        self.assertIn("Fast {output.toUpperCase()} encoding", source)
        self.assertIn('output === "png" || output === "webp"', source)
        self.assertIn("<FieldLegend>Page cleanup</FieldLegend>", source)
        self.assertIn("Fast {output.toUpperCase()} encoding", desktop_source)
        self.assertIn('output === "png" || output === "webp"', desktop_source)
        self.assertIn("<FieldLegend>Page cleanup</FieldLegend>", desktop_source)
        self.assertIn('pdfMode === "print" && <Field><FieldLabel>Paper', desktop_source)
        self.assertIn(
            'pdfMode === "print" && <Field><FieldLabel>Orientation',
            desktop_source,
        )
        self.assertIn('"style": "radix-nova"', components)


class ValidationTests(unittest.TestCase):
    def test_selector_requires_viewport_capture(self):
        with self.assertRaises(ValidationError):
            RenderRequest(url="https://example.com", selector="main")

    def test_managed_header_is_rejected(self):
        with self.assertRaises(ValidationError):
            RenderRequest(url="https://example.com", headers={"Host": "internal"})

    def test_raw_input_headers_require_base_url(self):
        with self.assertRaises(ValidationError):
            RenderRequest(html="private", headers={"Authorization": "Bearer x"})
        request = RenderRequest(
            html="private",
            base_url="https://example.com",
            headers={"Authorization": "Bearer x"},
        )
        self.assertEqual(str(request.base_url), "https://example.com/")

    def test_valid_request(self):
        request = RenderRequest(
            url="https://example.com", full_page=False, selector="main"
        )
        self.assertEqual(request.source_type, "url")

    def test_preserve_viewport_width_requires_full_page(self):
        request = RenderRequest(
            url="https://example.com", preserve_viewport_width=True
        )
        self.assertTrue(request.preserve_viewport_width)
        with self.assertRaises(ValidationError):
            RenderRequest(
                url="https://example.com",
                full_page=False,
                preserve_viewport_width=True,
            )

    def test_lazy_load_modes_and_fast_png_are_valid(self):
        request = RenderRequest(
            url="https://example.com",
            output="png",
            lazy_load="adaptive",
            image={"optimize_for_speed": True},
        )
        self.assertIs(request.lazy_load, LazyLoadMode.ADAPTIVE)
        self.assertTrue(request.image.optimize_for_speed)

    def test_legacy_fast_webp_flag_remains_valid(self):
        request = RenderRequest(
            url="https://example.com",
            output="webp",
            image={"optimize_for_speed": True},
        )
        self.assertTrue(request.image.optimize_for_speed)

    def test_fast_encoding_is_rejected_for_jpeg(self):
        with self.assertRaises(ValidationError):
            RenderRequest(
                url="https://example.com",
                output="jpeg",
                image={"optimize_for_speed": True},
            )


class DimensionTests(unittest.TestCase):
    def test_dimensions_within_limits(self):
        ensure_dimensions(1280, 720, 1, RenderLimits())

    def test_dimension_limit_is_enforced(self):
        with self.assertRaisesRegex(RenderError, "output_dimensions_exceeded"):
            ensure_dimensions(
                2001, 1000, 1, RenderLimits(max_width=2000, max_height=1000)
            )

    def test_pixel_limit_is_enforced(self):
        with self.assertRaisesRegex(RenderError, "pixel_limit_exceeded"):
            ensure_dimensions(100, 100, 1, RenderLimits(max_pixels=9999))

    def test_full_page_height_uses_separate_safety_limit(self):
        limits = RenderLimits(
            max_width=1920,
            max_height=1080,
            max_pixels=50_000_000,
            max_full_page_height=20_000,
        )
        ensure_full_page_dimensions(1280, 4_000, 1, limits)
        with self.assertRaisesRegex(RenderError, "page_too_tall"):
            ensure_full_page_dimensions(1280, 20_001, 1, limits)

    def test_self_host_defaults_allow_large_local_captures(self):
        limits = RenderLimits()
        self.assertEqual(limits.max_width, 16_384)
        self.assertEqual(limits.max_height, 16_384)
        self.assertEqual(limits.max_pixels, 500_000_000)
        self.assertEqual(limits.max_full_page_height, 100_000)
        self.assertEqual(limits.output_bytes, 1024 * 1024 * 1024)
        request = RenderRequest(
            url="https://example.com",
            viewport={"width": 16_384, "height": 16_384},
            full_page=False,
        )
        ensure_dimensions(
            request.viewport.width,
            request.viewport.height,
            request.viewport.device_scale_factor,
            limits,
        )

    def test_wide_page_error_suggests_preserving_viewport_width(self):
        with self.assertRaises(RenderError) as raised:
            ensure_full_page_dimensions(
                4_000,
                1_000,
                1,
                RenderLimits(max_width=1920, max_height=1080),
                viewport_width=1280,
            )

        self.assertEqual(
            raised.exception.details["suggested_action"],
            "preserve_viewport_width",
        )


if __name__ == "__main__":
    unittest.main()
