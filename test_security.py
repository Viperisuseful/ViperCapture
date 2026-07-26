import asyncio
import socket
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from main import gpu_launch_args, hardware_gpu_active
from render_contract import LazyLoadMode, RenderRequest
from render_engine import (
    PublicUrlValidator,
    RenderLimits,
    capture_webp,
    ensure_dimensions,
    is_public_http_url,
    load_lazy_content,
    needs_request_routing,
    routed_headers,
)
from render_errors import RenderError


class SsrfTests(unittest.IsolatedAsyncioTestCase):
    @patch("render_engine.socket.getaddrinfo")
    async def test_private_address_is_blocked(self, getaddrinfo):
        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]
        self.assertFalse(await is_public_http_url("https://example.com"))

    @patch("render_engine.socket.getaddrinfo")
    async def test_public_address_is_allowed(self, getaddrinfo):
        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        self.assertTrue(await is_public_http_url("https://example.com"))

    @patch("render_engine.socket.getaddrinfo")
    async def test_mixed_dns_result_is_blocked(self, getaddrinfo):
        getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]
        self.assertFalse(await is_public_http_url("https://example.com"))

    @patch("render_engine._resolve_public_origin", new_callable=AsyncMock)
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

    @patch("render_engine.socket.getaddrinfo")
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
        with patch("render_engine.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await load_lazy_content(page, 720, LazyLoadMode.ADAPTIVE)

        sleep.assert_awaited_once_with(0.075)

    async def test_thorough_lazy_loading_preserves_existing_delay(self):
        page = AsyncMock()

        async def evaluate(script, *_args):
            return 720 if script.startswith("Math.max") else None

        page.evaluate.side_effect = evaluate
        with patch("render_engine.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await load_lazy_content(page, 720, LazyLoadMode.THOROUGH)

        sleep.assert_awaited_once_with(0.2)

    async def test_fast_webp_sets_cdp_speed_flag(self):
        session = AsyncMock()

        async def send(method, _params=None):
            return {"data": "eA=="} if method == "Page.captureScreenshot" else {}

        session.send.side_effect = send
        page = AsyncMock()
        page.context.new_cdp_session.return_value = session

        result = await capture_webp(
            page,
            clip={"x": 0, "y": 0, "width": 100, "height": 100, "scale": 1},
            quality=80,
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


class GpuConfigurationTests(unittest.TestCase):
    def test_gpu_is_off_by_default(self):
        self.assertEqual(gpu_launch_args("off", "default", "linux"), [])

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


class ValidationTests(unittest.TestCase):
    def test_selector_requires_viewport_capture(self):
        with self.assertRaises(ValidationError):
            RenderRequest(url="https://example.com", selector="main")

    def test_managed_header_is_rejected(self):
        with self.assertRaises(ValidationError):
            RenderRequest(url="https://example.com", headers={"Host": "internal"})

    def test_valid_request(self):
        request = RenderRequest(
            url="https://example.com", full_page=False, selector="main"
        )
        self.assertEqual(request.source_type, "url")

    def test_lazy_load_modes_and_fast_webp_are_valid(self):
        request = RenderRequest(
            url="https://example.com",
            output="webp",
            lazy_load="adaptive",
            image={"optimize_for_speed": True},
        )
        self.assertIs(request.lazy_load, LazyLoadMode.ADAPTIVE)
        self.assertTrue(request.image.optimize_for_speed)

    def test_fast_encoding_is_rejected_for_png(self):
        with self.assertRaises(ValidationError):
            RenderRequest(
                url="https://example.com",
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


if __name__ == "__main__":
    unittest.main()
