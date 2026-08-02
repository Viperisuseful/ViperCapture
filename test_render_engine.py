import asyncio
import io
import json
import socket
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from render_contract import LazyLoadMode, RenderRequest
from render_engine import (
    PublicUrlValidator,
    RenderEngine,
    RenderLimits,
    ensure_dimensions,
    ensure_full_page_dimensions,
    is_public_http_url,
    load_lazy_content,
    normalized_origin,
    render_metadata,
    routed_headers,
)
from render_errors import RenderError


class FakeNavigation:
    status = 200


class FakeLocator:
    def __init__(self, page):
        self.page = page
        self.first = self

    async def wait_for(self, **options):
        self.page.wait_options.append(options)

    async def is_visible(self):
        return True

    async def bounding_box(self):
        return {"x": 5, "y": 6, "width": 320, "height": 200}

    async def screenshot(self, **options):
        self.page.screenshot_options = options
        return b"selector-image"


class FakePage:
    def __init__(self, target="https://example.com/"):
        self.url = target
        self.goto_options = {}
        self.wait_options = []
        self.screenshot_options = {}
        self.waited_text = None
        self.delay = None
        self.styles = []

    def on(self, *_args):
        return None

    async def goto(self, url, **options):
        self.url = url
        self.goto_options = options
        return FakeNavigation()

    def locator(self, _selector):
        return FakeLocator(self)

    async def wait_for_function(self, _script, *, arg, **_options):
        self.waited_text = arg

    async def wait_for_timeout(self, delay):
        self.delay = delay

    async def add_style_tag(self, *, content):
        self.styles.append(content)

    async def evaluate(self, script, *_args):
        if "width:" in script and "height:" in script:
            return {"width": 640, "height": 480}
        return 480

    async def screenshot(self, **options):
        self.screenshot_options = options
        return b"page-image"


class FakeContext:
    def __init__(self, page=None):
        self.page = page or FakePage()
        self.closed = False
        self.route_handler = None
        self.websocket_handler = None
        self.init_scripts = []

    async def route(self, _pattern, handler):
        self.route_handler = handler

    async def route_web_socket(self, _pattern, handler):
        self.websocket_handler = handler

    async def new_page(self):
        return self.page

    async def add_init_script(self, *, script):
        self.init_scripts.append(script)

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context=None):
        self.context = context or FakeContext()
        self.context_options = None

    async def new_context(self, **options):
        self.context_options = options
        return self.context

    def is_connected(self):
        return True


class RenderEngineTest(unittest.IsolatedAsyncioTestCase):
    def test_same_origin_headers_do_not_cross_redirects(self):
        original = "https://Example.com/path"
        headers = {"authorization": "old", "accept": "image/png"}
        custom = {"Authorization": "Bearer secret", "Cookie": "session=secret"}
        same = routed_headers("https://example.com:443/next", original, headers, custom)
        self.assertEqual(same["Authorization"], "Bearer secret")
        cross = routed_headers("https://cdn.example.com/asset", original, same, custom)
        self.assertNotIn("Authorization", cross)
        self.assertNotIn("Cookie", cross)
        self.assertEqual(normalized_origin(original), ("https", "example.com", 443))

    async def test_public_address_is_revalidated_without_dns_cache(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch(
            "render_engine.socket.getaddrinfo", side_effect=(public, private)
        ) as resolve:
            self.assertTrue(await is_public_http_url("https://example.com"))
            self.assertFalse(await is_public_http_url("https://example.com"))
        self.assertEqual(resolve.call_count, 2)

    async def test_public_address_resolution_has_a_hard_timeout(self):
        def slow_to_thread(*_args, **_kwargs):
            return asyncio.sleep(60)

        with (
            patch("render_engine.DNS_RESOLUTION_TIMEOUT_SECONDS", 0.01),
            patch("render_engine.asyncio.to_thread", new=slow_to_thread),
        ):
            self.assertFalse(await is_public_http_url("https://unresolvable.example"))

    async def test_simultaneous_public_address_checks_share_only_in_flight_work(self):
        validator = PublicUrlValidator()
        with patch(
            "render_engine._resolve_public_origin",
            AsyncMock(return_value=True),
        ) as resolve:
            allowed = await asyncio.gather(
                validator.is_public("https://example.com/a"),
                validator.is_public("https://example.com/b"),
            )
            self.assertEqual(allowed, [True, True])
            self.assertEqual(resolve.await_count, 1)
            self.assertTrue(await validator.is_public("https://example.com/c"))
            self.assertEqual(resolve.await_count, 2)

    async def test_lazy_load_modes_preserve_default_and_allow_fast_paths(self):
        class ScrollPage:
            def __init__(self):
                self.scrolls = []

            async def evaluate(self, _script, position=None):
                if position is not None:
                    self.scrolls.append(position)
                    return None
                return 2400

        none_page = ScrollPage()
        await load_lazy_content(none_page, 800, LazyLoadMode.NONE)
        self.assertEqual(none_page.scrolls, [])

        adaptive_page = ScrollPage()
        with patch("render_engine.asyncio.sleep", AsyncMock()) as sleep:
            await load_lazy_content(adaptive_page, 800, LazyLoadMode.ADAPTIVE)
        self.assertIn(800, adaptive_page.scrolls)
        self.assertEqual(adaptive_page.scrolls[-1], 0)
        self.assertTrue(
            all(call.args[0] == 0.075 for call in sleep.await_args_list[:-1])
        )

    def test_dimensions_enforce_quality_and_pixel_limits(self):
        limits = RenderLimits(max_width=1920, max_height=1080, max_pixels=2_073_600)
        ensure_dimensions(1920, 1080, 1, limits)
        with self.assertRaises(RenderError):
            ensure_dimensions(1921, 1080, 1, limits)
        with self.assertRaises(RenderError):
            ensure_dimensions(1920, 1080, 1.1, limits)

    def test_full_page_height_uses_safety_cap_not_viewport_cap(self):
        limits = RenderLimits(
            max_width=1920,
            max_height=1080,
            max_pixels=50_000_000,
            max_full_page_height=20_000,
        )
        ensure_full_page_dimensions(1280, 4_000, 1, limits)
        with self.assertRaises(RenderError):
            ensure_full_page_dimensions(1280, 20_001, 1, limits)

    def test_wide_page_error_suggests_preserving_viewport_width(self):
        limits = RenderLimits(
            max_width=1920,
            max_height=1080,
            max_pixels=50_000_000,
        )
        with self.assertRaises(RenderError) as raised:
            ensure_full_page_dimensions(
                4000,
                14000,
                1,
                limits,
                viewport_width=1280,
            )
        self.assertEqual(raised.exception.code, "output_dimensions_exceeded")
        self.assertEqual(
            raised.exception.details,
            {
                "max_width": 1920,
                "max_height": 1080,
                "page_width": 4000,
                "viewport_width": 1280,
                "suggested_action": "preserve_viewport_width",
            },
        )

    async def test_preserved_width_clips_horizontal_overflow_and_keeps_full_height(
        self,
    ):
        class WidePage(FakePage):
            async def evaluate(self, script, *_args):
                if "width:" in script and "height:" in script:
                    return {"width": 4000, "height": 14000}
                return 14000

        page = WidePage()
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "viewport": {"width": 1280, "height": 720},
                "full_page": True,
                "preserve_viewport_width": True,
                "lazy_load": "none",
            }
        )
        with patch(
            "render_engine.capture_clipped_image",
            AsyncMock(return_value=b"page-image"),
        ) as clipped:
            artifact = await RenderEngine(hosted=False).render_image(
                FakeBrowser(FakeContext(page)),
                request,
                RenderLimits(
                    max_width=1920,
                    max_height=1080,
                    max_pixels=50_000_000,
                ),
            )
        self.assertEqual(
            clipped.await_args.kwargs["clip"],
            {
                "x": 0,
                "y": 0,
                "width": 1280.0,
                "height": 14000.0,
                "scale": 1,
            },
        )
        self.assertEqual(clipped.await_args.kwargs["output"].value, "png")
        self.assertEqual(artifact.metadata["width"], 1280)
        self.assertEqual(artifact.metadata["height"], 14000)
        self.assertEqual(artifact.metadata["navigation_status"], 200)

    async def test_environment_css_status_and_clip_are_applied(self):
        page = FakePage()
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "full_page": False,
                "environment": {
                    "device": "pixel_7",
                    "color_scheme": "dark",
                    "reduced_motion": "reduce",
                    "locale": "fr-FR",
                    "timezone": "Europe/Paris",
                },
                "custom_css": "nav { display: none }",
                "clip": {"x": 5, "y": 6, "width": 320, "height": 200},
            }
        )
        browser = FakeBrowser(FakeContext(page))
        descriptor = {
            "Pixel 7": {
                "user_agent": "Pixel Test",
                "has_touch": True,
                "is_mobile": True,
            }
        }
        with patch(
            "render_engine.capture_clipped_image",
            AsyncMock(return_value=b"clip-image"),
        ) as clipped:
            artifact = await RenderEngine(
                hosted=False, device_descriptors=descriptor
            ).render(
                browser,
                request,
                RenderLimits(max_width=1920, max_height=1080, max_pixels=10_000_000),
            )
        self.assertEqual(page.styles, ["nav { display: none }"])
        self.assertEqual(browser.context_options["user_agent"], "Pixel Test")
        self.assertEqual(browser.context_options["color_scheme"], "dark")
        self.assertEqual(browser.context_options["timezone_id"], "Europe/Paris")
        self.assertIn("Linux armv8l", browser.context.init_scripts[0])
        self.assertEqual(clipped.await_args.kwargs["clip"]["x"], 5)
        self.assertEqual(artifact.metadata["width"], 320)

    async def test_configured_navigation_status_fails(self):
        request = RenderRequest.model_validate(
            {"url": "https://example.com", "fail_on_status": [200]}
        )
        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False).render(
                FakeBrowser(),
                request,
                RenderLimits(max_width=1920, max_height=1080, max_pixels=10_000_000),
            )
        self.assertEqual(raised.exception.code, "target_status_failed")
        self.assertEqual(
            raised.exception.headers["X-ViperCapture-Navigation-Status"], "200"
        )

    async def test_multi_viewport_pack_is_bounded_and_manifested(self):
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "full_page": False,
                "viewports": [
                    {"name": "desktop", "width": 800, "height": 600},
                    {"name": "mobile", "width": 390, "height": 720},
                ],
            }
        )
        artifact = await RenderEngine(hosted=False).render(
            FakeBrowser(),
            request,
            RenderLimits(max_width=1920, max_height=1080, max_pixels=10_000_000),
        )
        self.assertEqual(artifact.media_type, "application/zip")
        self.assertEqual(artifact.metadata["output_count"], 2)
        with zipfile.ZipFile(io.BytesIO(artifact.body)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"desktop.png", "mobile.png", "manifest.json"},
            )
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["count"], 2)

    async def test_metadata_output_is_bounded_json(self):
        class MetadataPage:
            async def evaluate(self, _script, _options):
                return {
                    "title": "Example",
                    "description": "Description",
                    "headings": [{"level": 1, "text": "Hello"}],
                    "links": {"total": 0, "sample": []},
                }

        artifact = await render_metadata(MetadataPage())
        self.assertEqual(artifact.media_type, "application/json")
        self.assertEqual(json.loads(artifact.body)["title"], "Example")

    async def test_selector_transparency_quality_and_waits(self):
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "output": "webp",
                "full_page": False,
                "selector": "main",
                "image": {
                    "quality": 82,
                    "transparent_background": True,
                    "optimize_for_speed": True,
                },
                "wait_for": {
                    "event": "networkidle",
                    "selector": "#ready",
                    "text": "Ready",
                    "delay_ms": 25,
                },
            }
        )
        context = FakeContext()
        browser = FakeBrowser(context)
        with patch(
            "render_engine.capture_webp", AsyncMock(return_value=b"selector-image")
        ) as webp:
            artifact = await RenderEngine(hosted=False).render_image(
                browser,
                request,
                RenderLimits(max_width=1920, max_height=1080, max_pixels=2_073_600),
            )
        self.assertEqual(artifact.body, b"selector-image")
        self.assertEqual(artifact.media_type, "image/webp")
        self.assertEqual(context.page.goto_options["wait_until"], "networkidle")
        self.assertEqual(context.page.waited_text, "Ready")
        self.assertEqual(context.page.delay, 25)
        self.assertEqual(webp.await_args.kwargs["quality"], 82)
        self.assertTrue(webp.await_args.kwargs["transparent"])
        self.assertTrue(webp.await_args.kwargs["optimize_for_speed"])
        self.assertEqual(webp.await_args.kwargs["clip"]["width"], 320)
        self.assertEqual(
            browser.context_options["screen"], {"width": 1280, "height": 720}
        )
        self.assertTrue(context.closed)

    async def test_context_closes_when_render_fails(self):
        class BrokenPage(FakePage):
            async def goto(self, url, **options):
                raise RuntimeError("browser failed")

        context = FakeContext(BrokenPage())
        request = RenderRequest.model_validate({"url": "https://example.com"})
        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False).render_image(
                FakeBrowser(context),
                request,
                RenderLimits(max_width=1920, max_height=1080, max_pixels=2_073_600),
            )
        self.assertEqual(raised.exception.code, "render_failed")
        self.assertTrue(context.closed)

    async def test_captcha_preference_reaches_every_challenge_check(self):
        checker = AsyncMock()
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "full_page": True,
                "proceed_on_captcha": True,
            }
        )
        await RenderEngine(hosted=False, challenge_checker=checker).render_image(
            FakeBrowser(),
            request,
            RenderLimits(max_width=1920, max_height=1080, max_pixels=2_073_600),
        )
        self.assertEqual(checker.await_count, 2)
        self.assertTrue(all(call.args[1] is True for call in checker.await_args_list))

    async def test_failed_context_cleanup_restarts_browser(self):
        class BrokenCloseContext(FakeContext):
            async def close(self):
                raise RuntimeError("close failed")

        context = BrokenCloseContext()
        replace = AsyncMock()
        request = RenderRequest.model_validate(
            {"url": "https://example.com", "full_page": False}
        )
        await RenderEngine(hosted=False, browser_replacer=replace).render_image(
            FakeBrowser(context),
            request,
            RenderLimits(max_width=1920, max_height=1080, max_pixels=2_073_600),
        )
        replace.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
