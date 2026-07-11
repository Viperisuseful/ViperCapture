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

    async def test_captcha_blocks_unless_explicitly_allowed(self):
        class Page:
            def __init__(self, provider):
                self.provider = provider
                self.script = ""

            async def evaluate(self, script):
                self.script = script
                return self.provider

        page = Page("cloudflare")
        with self.assertRaises(HTTPException) as raised:
            await main._check_captcha(page, False)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail,
            {"code": "captcha_detected", "provider": "cloudflare"},
        )
        self.assertIn("recaptcha", page.script)
        self.assertIn("hcaptcha", page.script)
        await main._check_captcha(Page("recaptcha"), True)
        await main._check_captcha(Page(None), False)

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
