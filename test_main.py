import unittest
from unittest.mock import patch

from fastapi import HTTPException

import main


class CaptureLimitsTest(unittest.IsolatedAsyncioTestCase):
    def test_url_rules_change_in_hosted_mode(self):
        self.assertEqual(main._validate_url("https://example.com"), "https://example.com")
        self.assertEqual(main._validate_url("file:///tmp/page.html"), "file:///tmp/page.html")
        with patch.object(main, "HOSTED", True):
            with self.assertRaises(HTTPException):
                main._validate_url("file:///etc/passwd")
        with self.assertRaises(HTTPException):
            main._validate_url("http://[invalid")

    async def test_private_hosts_are_not_public(self):
        self.assertFalse(await main._is_public_url("http://127.0.0.1", {}))
        self.assertFalse(await main._is_public_url("http://[::1]", {}))

    def test_output_pixel_budget(self):
        with patch.object(main, "MAX_SCREENSHOT_PIXELS", 100):
            main._ensure_pixel_budget(10, 10, 1)
            with self.assertRaises(HTTPException) as raised:
                main._ensure_pixel_budget(10, 10, 1.1)
        self.assertEqual(raised.exception.status_code, 413)

    async def test_app_config_exposes_hosted_limits(self):
        with (
            patch.object(main, "HOSTED", True),
            patch.object(main, "MAX_SCREENSHOT_PIXELS", 123_456),
        ):
            config = await main.app_config()
        self.assertEqual(
            config,
            {"server_saves": False, "max_screenshot_pixels": 123_456},
        )

    async def test_lazy_scroll_handles_growth_caps_work_and_returns_to_top(self):
        class Page:
            def __init__(self, heights):
                self.heights = iter(heights)
                self.height_reads = 0
                self.scrolls = []

            async def evaluate(self, _script, position=None):
                if position is not None:
                    self.scrolls.append(position)
                    return None
                self.height_reads += 1
                return next(self.heights)

        with patch.object(main, "LAZY_SCROLL_DELAY_SECONDS", 0):
            growing = Page([1000, 1800, 1800, 2600, 2600, 2600])
            await main._load_lazy_content(growing, 1000)
            self.assertIn(1600, growing.scrolls)
            self.assertEqual(growing.scrolls[-1], 0)

            endless = Page(1000 * step for step in range(1, 100))
            with patch.object(main, "LAZY_SCROLL_MAX_STEPS", 3):
                await main._load_lazy_content(endless, 1000)
            self.assertEqual(endless.height_reads, 3)
            self.assertEqual(endless.scrolls[-1], 0)

    async def test_detector_v2_blocks_only_page_level_challenges(self):
        class Page:
            def __init__(self, result):
                self.result = result
                self.script = ""

            async def evaluate(self, script, _argument=None):
                self.script = script
                return self.result

        challenge = {
            "provider": "cloudflare",
            "kind": "blocking_interstitial",
            "confidence": 0.98,
            "signals": ["challenge_form", "viewport_obstruction"],
        }
        page = Page(challenge)
        with self.assertRaises(HTTPException) as raised:
            await main._check_captcha(page, False)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail,
            {
                "code": "captcha_detected",
                **challenge,
                "message": "Cloudflare challenge blocked the page",
            },
        )
        self.assertIn("recaptcha", page.script)
        self.assertIn("hcaptcha", page.script)
        self.assertIn("funcaptcha", page.script)
        self.assertIn("embedded_widget", page.script)
        await main._check_captcha(Page(challenge), True)
        await main._check_captcha(
            Page({"provider": "recaptcha", "kind": "embedded_widget"}),
            False,
        )
        await main._check_captcha(Page(None), False)

    def test_png_jpeg_and_webp_capture_options(self):
        clip = {"x": 0, "y": 0, "width": 100, "height": 50, "scale": 2}
        png = main._capture_options("png", clip)
        jpeg = main._capture_options("jpeg", clip)
        webp = main._capture_options("webp", clip)

        self.assertEqual(png["format"], "png")
        self.assertNotIn("quality", png)
        self.assertEqual(jpeg["format"], "jpeg")
        self.assertEqual(jpeg["quality"], 90)
        self.assertEqual(webp["format"], "webp")
        self.assertEqual(webp["quality"], 90)
        self.assertEqual(main._media_type("jpeg"), "image/jpeg")
        self.assertEqual(main._safe_filename("capture.png", "webp"), "capture.webp")

    def test_public_ui_has_formats_without_hosted_cleanup_controls(self):
        html = (main.BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('name="format"', html)
        self.assertNotIn('name="consent_mode"', html)
        self.assertNotIn('name="block_ads"', html)
        self.assertNotIn("autoconsent", html.lower())

    async def test_downloads_button_uses_windows_known_folder(self):
        with (
            patch.dict(main.os.environ, {}, clear=False),
            patch.object(main.sys, "platform", "win32"),
            patch.object(main.subprocess, "Popen") as open_folder,
        ):
            main.os.environ.pop("SHOT_DOWNLOADS_DIR", None)
            result = await main.open_downloads_folder()

        open_folder.assert_called_once_with(["explorer.exe", "shell:Downloads"])
        self.assertTrue(result["opened"])


if __name__ == "__main__":
    unittest.main()
