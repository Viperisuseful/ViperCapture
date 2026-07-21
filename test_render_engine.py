import socket
import unittest
from unittest.mock import AsyncMock, patch

from render_contract import RenderRequest
from render_engine import RenderEngine, RenderLimits, is_public_http_url, routed_headers
from render_errors import RenderError


class FakeNavigation:
    status = 200


class FakeLocator:
    def __init__(self, page):
        self.page = page
        self.first = self

    async def wait_for(self, **_options):
        return None

    async def is_visible(self):
        return True

    async def bounding_box(self):
        return {"x": 0, "y": 0, "width": 300, "height": 180}

    async def screenshot(self, **options):
        self.page.options = options
        return b"open-image"


class FakePage:
    def __init__(self):
        self.url = "https://example.com/"
        self.options = {}

    async def goto(self, url, **_options):
        self.url = url
        return FakeNavigation()

    def locator(self, _selector):
        return FakeLocator(self)

    async def wait_for_function(self, *_args, **_kwargs):
        return None

    async def wait_for_timeout(self, _delay):
        return None

    async def evaluate(self, _script, *_args):
        if "scrollHeight" in _script and "{width" not in _script:
            return 480
        return {"width": 640, "height": 480}

    async def screenshot(self, **options):
        self.options = options
        return b"open-image"


class FakeContext:
    def __init__(self, page=None):
        self.page = page or FakePage()
        self.closed = False

    async def route(self, _pattern, _handler):
        return None

    async def route_web_socket(self, _pattern, _handler):
        return None

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context):
        self.context = context

    async def new_context(self, **_options):
        return self.context

    def is_connected(self):
        return True


class RenderEngineTest(unittest.IsolatedAsyncioTestCase):
    def test_headers_are_exact_origin_only(self):
        custom = {"Authorization": "Bearer secret", "Cookie": "session=secret"}
        same = routed_headers("https://example.com/next", "https://example.com", {}, custom)
        self.assertEqual(same["Authorization"], "Bearer secret")
        cross = routed_headers("https://cdn.example.com", "https://example.com", same, custom)
        self.assertNotIn("Authorization", cross)
        self.assertNotIn("Cookie", cross)

    async def test_public_address_is_revalidated(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("render_engine.socket.getaddrinfo", side_effect=(public, private)) as resolve:
            self.assertTrue(await is_public_http_url("https://example.com"))
            self.assertFalse(await is_public_http_url("https://example.com"))
        self.assertEqual(resolve.call_count, 2)

    async def test_selector_quality_transparency_and_cleanup(self):
        request = RenderRequest.model_validate({
            "url": "https://example.com", "output": "webp", "full_page": False,
            "selector": "main", "image": {"quality": 80, "transparent_background": True},
        })
        context = FakeContext()
        with patch("render_engine.capture_webp", AsyncMock(return_value=b"open-image")) as webp:
            artifact = await RenderEngine(hosted=False).render_image(
                FakeBrowser(context), request, RenderLimits(max_width=1920, max_height=1080)
            )
        self.assertEqual(artifact.body, b"open-image")
        self.assertEqual(webp.await_args.kwargs["quality"], 80)
        self.assertTrue(webp.await_args.kwargs["transparent"])
        self.assertEqual(webp.await_args.kwargs["clip"]["width"], 300)
        self.assertTrue(context.closed)

    async def test_context_closes_after_failure(self):
        class BrokenPage(FakePage):
            async def goto(self, url, **_options):
                raise RuntimeError("failed")

        context = FakeContext(BrokenPage())
        with self.assertRaises(RenderError):
            await RenderEngine(hosted=False).render_image(
                FakeBrowser(context), RenderRequest(url="https://example.com"), RenderLimits()
            )
        self.assertTrue(context.closed)

    async def test_captcha_preference_reaches_challenge_check(self):
        checker = AsyncMock()
        await RenderEngine(hosted=False, challenge_checker=checker).render_image(
            FakeBrowser(FakeContext()),
            RenderRequest(url="https://example.com", proceed_on_captcha=True),
            RenderLimits(),
        )
        self.assertEqual(checker.await_count, 2)
        self.assertTrue(all(call.args[1] for call in checker.await_args_list))

    async def test_cleanup_failure_requests_browser_replacement(self):
        class BrokenCloseContext(FakeContext):
            async def close(self):
                raise RuntimeError("close failed")

        replace = AsyncMock()
        await RenderEngine(hosted=False, browser_replacer=replace).render_image(
            FakeBrowser(BrokenCloseContext()),
            RenderRequest(url="https://example.com", full_page=False),
            RenderLimits(),
        )
        replace.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
