import asyncio
import io
import json
import socket
import threading
import unittest
import zipfile
from base64 import b64encode
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as PlaywrightError

from render_contract import LazyLoadMode, OutputFormat, RenderRequest
from render_engine import (
    CleanupHooks,
    PublicUrlValidator,
    RenderArtifact,
    RenderEngine,
    RenderLimits,
    _resolve_public_origin,
    _run_process,
    capture_clipped_image,
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

    async def set_content(self, content, **options):
        self.content = content
        self.goto_options = options

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
        self.page.context = self
        self.closed = False
        self.route_handler = None
        self.websocket_handler = None
        self.init_scripts = []

    async def route(self, _pattern, handler):
        self.route_handler = handler

    async def route_web_socket(self, _pattern, handler):
        self.websocket_handler = handler

    async def new_page(self):
        self.page.context = self
        return self.page

    async def add_init_script(self, *, script):
        self.init_scripts.append(script)

    async def new_cdp_session(self, _page):
        class Session:
            async def send(self, method, _options=None):
                if method == "Page.captureScreenshot":
                    return {"data": b64encode(b"page-image").decode()}
                return {}

            async def detach(self):
                return None

        return Session()

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
    async def test_clipped_capture_hides_and_restores_caret(self):
        page = FakePage()
        page.context = FakeContext(page)
        evaluations = []

        async def evaluate(script, *_args):
            evaluations.append(script)

        page.evaluate = evaluate
        await capture_clipped_image(
            page,
            output=OutputFormat.PNG,
            clip={"x": 0, "y": 0, "width": 320, "height": 240, "scale": 1},
            quality=None,
            transparent=False,
        )
        self.assertIn("caret-color: transparent", evaluations[0])
        self.assertIn("animation.finish()", evaluations[0])
        self.assertIn("animation.cancel()", evaluations[0])
        self.assertIn("style[data-vipercapture-screenshot]", evaluations[-1])

    async def test_action_transport_failure_remains_retryable(self):
        class BrokenLocator(FakeLocator):
            async def click(self, **_options):
                raise PlaywrightError("page closed")

        class BrokenPage(FakePage):
            def locator(self, _selector):
                return BrokenLocator(self)

        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False).render(
                FakeBrowser(FakeContext(BrokenPage())),
                RenderRequest(
                    url="https://example.com",
                    actions=[{"type": "click", "selector": "button"}],
                ),
                RenderLimits(
                    max_width=1920,
                    max_height=1080,
                    max_pixels=2_073_600,
                ),
            )
        self.assertEqual(raised.exception.code, "render_failed")
        self.assertTrue(raised.exception.retryable)

    async def test_invalid_press_key_is_not_retryable(self):
        class BrokenLocator(FakeLocator):
            async def press(self, *_args, **_options):
                raise PlaywrightError("Unknown key: Control+")

        class BrokenPage(FakePage):
            def locator(self, _selector):
                return BrokenLocator(self)

        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False).render(
                FakeBrowser(FakeContext(BrokenPage())),
                RenderRequest(
                    url="https://example.com",
                    actions=[
                        {
                            "type": "press",
                            "selector": "input",
                            "key": "Control+",
                        }
                    ],
                ),
                RenderLimits(
                    max_width=1920,
                    max_height=1080,
                    max_pixels=2_073_600,
                ),
            )
        self.assertEqual(raised.exception.code, "action_key_invalid")
        self.assertFalse(raised.exception.retryable)

    async def test_custom_css_transport_failure_remains_retryable(self):
        class BrokenPage(FakePage):
            async def add_style_tag(self, **_options):
                raise PlaywrightError("page closed")

        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False).render(
                FakeBrowser(FakeContext(BrokenPage())),
                RenderRequest(
                    url="https://example.com", custom_css="body {}"
                ),
                RenderLimits(
                    max_width=1920,
                    max_height=1080,
                    max_pixels=2_073_600,
                ),
            )
        self.assertEqual(raised.exception.code, "render_failed")
        self.assertTrue(raised.exception.retryable)

    async def test_malformed_capture_selector_is_not_retryable(self):
        class BrokenLocator(FakeLocator):
            async def is_visible(self):
                raise PlaywrightError(
                    "Unexpected token while parsing css selector"
                )

        class BrokenPage(FakePage):
            def locator(self, _selector):
                return BrokenLocator(self)

        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False).render(
                FakeBrowser(FakeContext(BrokenPage())),
                RenderRequest(
                    url="https://example.com",
                    full_page=False,
                    selector="div[",
                ),
                RenderLimits(
                    max_width=1920,
                    max_height=1080,
                    max_pixels=2_073_600,
                ),
            )
        self.assertEqual(raised.exception.code, "selector_invalid")
        self.assertFalse(raised.exception.retryable)

    async def test_malformed_wait_selector_is_not_retryable(self):
        class BrokenLocator(FakeLocator):
            async def wait_for(self, **_options):
                raise PlaywrightError(
                    "Unexpected token while parsing css selector"
                )

        class BrokenPage(FakePage):
            def locator(self, _selector):
                return BrokenLocator(self)

        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False).render(
                FakeBrowser(FakeContext(BrokenPage())),
                RenderRequest(
                    url="https://example.com",
                    wait_for={"selector": "div["},
                ),
                RenderLimits(
                    max_width=1920,
                    max_height=1080,
                    max_pixels=2_073_600,
                ),
            )
        self.assertEqual(raised.exception.code, "wait_selector_invalid")
        self.assertFalse(raised.exception.retryable)

    async def test_full_page_capture_uses_validated_rectangle(self):
        page = FakePage()
        with patch(
            "render_engine.capture_clipped_image",
            AsyncMock(return_value=b"full-page"),
        ) as capture:
            await RenderEngine(hosted=False).render(
                FakeBrowser(FakeContext(page)),
                RenderRequest(url="https://example.com", full_page=True),
                RenderLimits(
                    max_width=1920,
                    max_height=1080,
                    max_pixels=10_000_000,
                ),
            )
        self.assertEqual(
            capture.await_args.kwargs["clip"],
            {
                "x": 0,
                "y": 0,
                "width": 1280.0,
                "height": 720.0,
                "scale": 1,
            },
        )

    async def test_dns_slot_is_held_until_timed_out_thread_finishes(self):
        started = threading.Event()
        release = threading.Event()

        def resolve(*_args, **_kwargs):
            started.set()
            release.wait()
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("93.184.216.34", 443),
                )
            ]

        slots = asyncio.Semaphore(1)
        with (
            patch("render_engine.PUBLIC_DNS_SLOTS", slots),
            patch("render_engine.DNS_RESOLUTION_TIMEOUT_SECONDS", 0.01),
            patch("render_engine.socket.getaddrinfo", side_effect=resolve),
        ):
            self.assertFalse(
                await _resolve_public_origin("first.example", 443)
            )
            second = asyncio.create_task(
                _resolve_public_origin("second.example", 443)
            )
            await asyncio.sleep(0.02)
            self.assertFalse(second.done())
            release.set()
            self.assertTrue(await second)

    async def test_default_self_hosted_render_skips_request_routing(self):
        context = FakeContext()
        await RenderEngine(hosted=False).render(
            FakeBrowser(context),
            RenderRequest(url="https://example.com"),
            RenderLimits(max_width=1920, max_height=1080, max_pixels=10_000_000),
        )
        self.assertIsNone(context.route_handler)

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

    async def test_blocked_resources_are_counted_without_aborting_main_document(self):
        class RoutedRequest:
            def __init__(self, url, resource_type, *, main=False):
                self.url = url
                self.resource_type = resource_type
                self.headers = {}
                self.frame = SimpleNamespace(
                    parent_frame=None if main else object()
                )
                self.main = main

            def is_navigation_request(self):
                return self.main

        class Route:
            def __init__(self, request):
                self.request = request
                self.aborted = False
                self.continued = False

            async def abort(self, _reason):
                self.aborted = True

            async def continue_(self, **_kwargs):
                self.continued = True

        context = FakeContext()

        class RoutingPage(FakePage):
            async def goto(self, url, **options):
                main_route = Route(RoutedRequest(url, "document", main=True))
                script_route = Route(
                    RoutedRequest(f"{url}/application.js", "script")
                )
                await context.route_handler(main_route)
                await context.route_handler(script_route)
                self.main_route = main_route
                self.script_route = script_route
                return await super().goto(url, **options)

        page = RoutingPage()
        context.page = page
        artifact = await RenderEngine(hosted=False).render(
            FakeBrowser(context),
            RenderRequest.model_validate(
                {
                    "url": "https://example.com",
                    "network": {
                        "block_resource_types": ["document", "script"],
                        "block_url_patterns": ["**/*"],
                    },
                }
            ),
            RenderLimits(max_width=1920, max_height=1080, max_pixels=10_000_000),
        )
        self.assertTrue(page.main_route.continued)
        self.assertFalse(page.main_route.aborted)
        self.assertTrue(page.script_route.aborted)
        self.assertEqual(artifact.metadata["blocked_subresources"], 1)

    async def test_self_hosted_websocket_block_controls_are_honored(self):
        class Socket:
            url = "wss://example.com/live"

            def __init__(self):
                self.closed = False
                self.connected = False

            async def close(self, **_kwargs):
                self.closed = True

            async def connect_to_server(self):
                self.connected = True

        context = FakeContext()
        socket_route = Socket()

        class SocketPage(FakePage):
            async def goto(self, url, **options):
                await context.websocket_handler(socket_route)
                return await super().goto(url, **options)

        context.page = SocketPage()
        browser = FakeBrowser(context)
        artifact = await RenderEngine(hosted=False).render(
            browser,
            RenderRequest.model_validate(
                {
                    "url": "https://example.com",
                    "network": {"block_url_patterns": ["*/live"]},
                }
            ),
            RenderLimits(max_width=1920, max_height=1080, max_pixels=10_000_000),
        )
        self.assertTrue(socket_route.closed)
        self.assertFalse(socket_route.connected)
        self.assertEqual(artifact.metadata["blocked_subresources"], 1)
        self.assertEqual(browser.context_options["service_workers"], "block")

    async def test_cleanup_blocks_matching_websocket(self):
        socket_route = SimpleNamespace(
            url="wss://chat.example/socket",
            close=AsyncMock(),
            connect_to_server=AsyncMock(),
        )
        context = FakeContext()

        class SocketPage(FakePage):
            async def goto(self, url, **options):
                await context.websocket_handler(socket_route)
                return await super().goto(url, **options)

        context.page = SocketPage()
        hooks = CleanupHooks(
            setup=AsyncMock(return_value=None),
            finish=AsyncMock(return_value={}),
            apply=AsyncMock(return_value={}),
            blocked_category=lambda url, _options: (
                "chat" if "chat.example" in url else None
            ),
        )
        await RenderEngine(hosted=False, cleanup_hooks=hooks).render(
            FakeBrowser(context),
            RenderRequest(
                url="https://example.com",
                cleanup={"block_chats": True},
            ),
            RenderLimits(max_width=1920, max_height=1080, max_pixels=10_000_000),
        )

        socket_route.close.assert_awaited_once()
        socket_route.connect_to_server.assert_not_awaited()

    async def test_inactive_cleanup_hooks_allow_service_workers(self):
        hooks = CleanupHooks(
            setup=AsyncMock(return_value=None),
            finish=AsyncMock(return_value={}),
            apply=AsyncMock(return_value={}),
            blocked_category=lambda _url, _options: None,
        )
        browser = FakeBrowser()

        await RenderEngine(hosted=False, cleanup_hooks=hooks).render(
            browser,
            RenderRequest(
                url="https://example.com",
                cleanup={
                    "consent_mode": "reject",
                    "block_newsletters": True,
                },
            ),
            RenderLimits(max_width=1920, max_height=1080, max_pixels=10_000_000),
        )

        self.assertEqual(browser.context_options["service_workers"], "allow")
        self.assertIsNone(browser.context.route_handler)

    async def test_policy_block_does_not_mask_later_render_failure(self):
        context = FakeContext()

        class FailingPage(FakePage):
            async def goto(self, url, **options):
                route = Route(RoutedRequest(f"{url}/blocked.js", "script"))
                await context.route_handler(route)
                return await super().goto(url, **options)

            async def screenshot(self, **_options):
                raise RuntimeError("browser disconnected")

        context.page = FailingPage()
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "network": {"block_resource_types": ["script"]},
            }
        )

        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False).render(
                FakeBrowser(context),
                request,
                RenderLimits(
                    max_width=1920,
                    max_height=1080,
                    max_pixels=10_000_000,
                ),
            )
        self.assertEqual(raised.exception.code, "render_failed")
        self.assertTrue(raised.exception.retryable)

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

    async def test_public_address_checks_cap_distinct_origins(self):
        validator = PublicUrlValidator()
        with (
            patch("render_engine.MAX_DNS_ORIGINS", 1),
            patch(
                "render_engine._resolve_public_origin",
                AsyncMock(return_value=True),
            ) as resolve,
        ):
            self.assertTrue(await validator.is_public("https://one.example"))
            self.assertFalse(await validator.is_public("https://two.example"))
        self.assertEqual(resolve.await_count, 1)

    async def test_cancelled_process_is_terminated_and_awaited(self):
        waiting = asyncio.Event()

        class Process:
            returncode = None

            def __init__(self):
                self.terminated = False
                self.waited = False

            async def communicate(self):
                await waiting.wait()

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            async def wait(self):
                self.waited = True
                return self.returncode

        process = Process()
        with patch(
            "render_engine.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            task = asyncio.create_task(_run_process(["ffmpeg"], 30))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)

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

    async def test_empty_live_request_match_set_is_preserved(self):
        matched: set[str] = set()
        pattern = "https://example.com/failure*"

        class MatchingPage:
            async def evaluate(self, *_args):
                matched.add(pattern)
                return True

        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "assertions": {
                    "content_includes": ["ready"],
                    "request_failures": [pattern],
                },
            }
        )
        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False)._check_assertions(
                MatchingPage(), request, [], matched
            )
        self.assertEqual(raised.exception.code, "request_assertion_failed")

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

    async def test_multi_viewport_pack_rejects_aggregate_before_archive(self):
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "full_page": False,
                "viewports": [
                    {"name": "one", "width": 10, "height": 10},
                    {"name": "two", "width": 10, "height": 10},
                ],
            }
        )
        engine = RenderEngine(hosted=False)
        with patch.object(
            engine,
            "_render_single",
            AsyncMock(return_value=RenderArtifact(b"123", "image/png", "x.png", {})),
        ), self.assertRaises(RenderError) as raised:
            await engine.render(
                FakeBrowser(), request, RenderLimits(output_bytes=5)
            )
        self.assertEqual(raised.exception.code, "output_too_large")

    async def test_assertion_match_survives_bounded_diagnostic_sample(self):
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "assertions": {"request_failures": ["*/critical.js"]},
            }
        )
        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False)._check_assertions(
                FakePage(),
                request,
                [{"url": "https://example.com/noise"}] * 200,
                {"*/critical.js"},
            )
        self.assertEqual(raised.exception.code, "request_assertion_failed")

    async def test_metadata_output_is_bounded_json(self):
        class MetadataPage:
            async def evaluate(self, _script, _options):
                return {
                    "title": "Example",
                    "description": "Description",
                    "headings": [{"level": 1, "text": "Hello"}],
                    "links": {"total": 0, "sample": []},
                    "images": {
                        "total": 1,
                        "sample": [
                            {
                                "src": "https://example.com/image.png",
                                "alt": "Example",
                                "width": 640,
                                "height": 480,
                            }
                        ],
                    },
                }

        artifact = await render_metadata(MetadataPage())
        self.assertEqual(artifact.media_type, "application/json")
        document = json.loads(artifact.body)
        self.assertEqual(document["title"], "Example")
        self.assertEqual(document["images"]["total"], 1)

    async def test_markdown_input_conversion_is_off_thread_and_bounded(self):
        request = RenderRequest.model_validate(
            {"markdown": "# Hello", "full_page": False}
        )
        with patch(
            "render_engine.asyncio.to_thread",
            new_callable=AsyncMock,
            wraps=asyncio.to_thread,
        ) as to_thread:
            await RenderEngine(hosted=False).render(
                FakeBrowser(), request, RenderLimits(output_bytes=1024)
            )
        self.assertTrue(
            any(
                call.args and call.args[0].__name__ == "input_document"
                for call in to_thread.await_args_list
            )
        )

        with patch("content_rendering.input_document", return_value="x" * 6):
            with self.assertRaises(RenderError) as raised:
                await RenderEngine(hosted=False).render(
                    FakeBrowser(), request, RenderLimits(output_bytes=5)
                )
        self.assertEqual(raised.exception.code, "document_too_large")

    async def test_cancelled_markdown_input_conversion_settles_thread(self):
        started = threading.Event()
        release = threading.Event()

        def blocked_input(_request):
            started.set()
            release.wait(timeout=2)
            return "<p>markdown</p>"

        with patch("content_rendering.input_document", side_effect=blocked_input):
            operation = asyncio.create_task(
                RenderEngine(hosted=False).render(
                    FakeBrowser(),
                    RenderRequest(markdown="# title"),
                    RenderLimits(
                        max_width=1920,
                        max_height=1080,
                        max_pixels=2_073_600,
                    ),
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            operation.cancel()
            await asyncio.sleep(0)
            self.assertFalse(operation.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation

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

    async def test_video_rechecks_captcha_after_recording(self):
        class VideoPage(FakePage):
            video = object()
            recorded = False

            async def wait_for_timeout(self, delay):
                await super().wait_for_timeout(delay)
                self.recorded = True

        async def checker(page, _proceed, _status):
            if page.recorded:
                raise RenderError(
                    "captcha_detected", "Challenge appeared.", 422, False
                )

        with self.assertRaises(RenderError) as raised:
            await RenderEngine(
                hosted=False, challenge_checker=checker
            ).render_image(
                FakeBrowser(FakeContext(VideoPage())),
                RenderRequest(
                    url="https://example.com",
                    output="webm",
                    full_page=False,
                    video={"duration_ms": 1_000},
                ),
                RenderLimits(
                    max_width=1920,
                    max_height=1080,
                    max_pixels=2_073_600,
                ),
            )
        self.assertEqual(raised.exception.code, "captcha_detected")

    async def test_cancelled_video_read_settles_before_cleanup(self):
        started = threading.Event()
        release = threading.Event()

        class Video:
            async def path(self):
                return "/tmp/source.webm"

        class VideoPage(FakePage):
            def __init__(self):
                super().__init__()
                self.video = Video()

            async def close(self):
                return None

        async def trim(_source, target, *, duration_ms):
            target.write_bytes(b"webm")
            return duration_ms

        def blocked_read():
            started.set()
            release.wait(timeout=2)
            return b"webm"

        with (
            patch("render_engine._trim_webm", side_effect=trim),
            patch("pathlib.Path.read_bytes", side_effect=blocked_read),
        ):
            operation = asyncio.create_task(
                RenderEngine(hosted=False).render_image(
                    FakeBrowser(FakeContext(VideoPage())),
                    RenderRequest(
                        url="https://example.com",
                        output="webm",
                        full_page=False,
                        video={"duration_ms": 1_000},
                    ),
                    RenderLimits(
                        max_width=1920,
                        max_height=1080,
                        max_pixels=2_073_600,
                    ),
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            operation.cancel()
            await asyncio.sleep(0)
            self.assertFalse(operation.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation

    async def test_assertions_run_after_full_page_lazy_loading(self):
        events = []

        async def lazy(*_args):
            events.append("lazy")

        async def assertions(*_args):
            events.append("assertions")

        request = RenderRequest.model_validate(
            {"url": "https://example.com", "full_page": True}
        )
        engine = RenderEngine(hosted=False)
        with (
            patch("render_engine.load_lazy_content", side_effect=lazy),
            patch.object(engine, "_check_assertions", side_effect=assertions),
        ):
            await engine.render_image(
                FakeBrowser(),
                request,
                RenderLimits(
                    max_width=1920,
                    max_height=1080,
                    max_pixels=2_073_600,
                ),
            )
        self.assertEqual(events, ["lazy", "assertions"])

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
