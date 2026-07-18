import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main


class CaptureLimitsTest(unittest.IsolatedAsyncioTestCase):
    async def test_v1_route_returns_engine_artifact(self):
        browser = SimpleNamespace(is_connected=lambda: True)
        main.app.state.browser = browser
        main.app.state.capture_slots = asyncio.Semaphore(1)
        artifact = SimpleNamespace(
            body=b"image-body",
            media_type="image/png",
            filename="vipercapture.png",
        )
        engine = SimpleNamespace(render_image=AsyncMock(return_value=artifact))
        with patch.object(main, "RenderEngine", return_value=engine):
            response = await main.render_v1(
                main.RenderRequest.model_validate(
                    {"url": "https://example.com", "full_page": False}
                )
            )
        self.assertEqual(response.body, b"image-body")
        self.assertEqual(response.media_type, "image/png")
        self.assertIn("vipercapture.png", response.headers["content-disposition"])
        engine.render_image.assert_awaited_once()

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
        with self.assertRaises(main.RenderError) as raised:
            await main._check_captcha(page, False)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.code, "captcha_detected")
        self.assertEqual(raised.exception.details, challenge)
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

    def test_public_ui_uses_v1_image_contract(self):
        html = (main.BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
        script = (main.BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('name="format"', html)
        for name in ("capture_mode", "selector", "quality", "transparent_background", "wait_event", "wait_selector", "wait_text", "headers"):
            self.assertIn(f'name="{name}"', html)
        self.assertIn('fetch("/v1/render", {', script)
        self.assertIn('method: "POST"', script)
        self.assertIn('body: JSON.stringify(payload)', script)
        self.assertNotIn("/screenshot", script)

    def test_legacy_route_is_removed(self):
        paths = {
            (route.path, tuple(getattr(route, "methods", ()) or ()))
            for route in main.app.routes
        }
        self.assertFalse(any(path == "/screenshot" for path, _methods in paths))
        self.assertIn(("/v1/render", ("POST",)), paths)
        response = TestClient(main.app, raise_server_exceptions=False).get("/screenshot")
        self.assertIn(response.status_code, {404, 405})

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
