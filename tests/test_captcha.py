import asyncio
import unittest
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from vipercapture.captcha import detect_challenge, handle_challenge
from vipercapture.render_errors import RenderError

BLOCKING = {
    "provider": "cloudflare",
    "kind": "blocking_interstitial",
    "confidence": 0.98,
    "signals": ["challenge_form", "viewport_obstruction"],
}


class CaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_detection_avoids_url_widget_and_shadow_limit_false_positives(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 800, "height": 600})
            try:
                await page.goto(
                    "data:text/html,<title>Verified</title><p>Success</p>#verify-email"
                )
                self.assertIsNone(await detect_challenge(page, 200))

                await page.set_content(
                    '<form><div class="frc-captcha" data-sitekey="public" '
                    'style="width:300px;height:80px"></div></form>'
                )
                friendly = await detect_challenge(page, 200)
                self.assertEqual(friendly["kind"], "embedded_widget")

                await page.set_content("<main></main>")
                await page.evaluate(
                    """() => {
                        for (let index = 0; index < 300; index += 1) {
                            const host = document.createElement("div");
                            const root = host.attachShadow({mode: "open"});
                            if (index === 299) {
                                root.innerHTML = '<div id="challenge-stage" style="width:800px;height:600px"></div>';
                            }
                            document.body.append(host);
                        }
                    }"""
                )
                self.assertIsNone(await detect_challenge(page, 200))
            finally:
                await browser.close()

    async def test_plain_forbidden_response_is_not_mislabeled_as_captcha(self):
        page = AsyncMock()
        page.evaluate.return_value = None
        await handle_challenge(
            page,
            navigation_status=403,
            action="error",
            handler=None,
            solver=None,
            timeout_ms=1_000,
        )

    async def test_embedded_widget_does_not_block_capture(self):
        page = AsyncMock()
        page.evaluate.return_value = {
            "provider": "recaptcha",
            "kind": "embedded_widget",
            "confidence": 0.72,
            "signals": ["provider_widget"],
        }
        await handle_challenge(
            page,
            navigation_status=200,
            action="error",
            handler=None,
            solver=None,
            timeout_ms=1_000,
        )

    async def test_blocking_challenge_returns_structured_detection(self):
        page = AsyncMock()
        page.evaluate.return_value = BLOCKING
        with self.assertRaises(RenderError) as raised:
            await handle_challenge(
                page,
                navigation_status=403,
                action="error",
                handler=None,
                solver=None,
                timeout_ms=1_000,
            )
        self.assertEqual(raised.exception.code, "captcha_detected")
        self.assertEqual(raised.exception.details["provider"], "cloudflare")

    async def test_external_handler_can_clear_a_challenge(self):
        page = AsyncMock()
        page.evaluate.side_effect = [BLOCKING, None]
        handler = AsyncMock(return_value=True)
        await handle_challenge(
            page,
            navigation_status=403,
            action="external",
            handler=handler,
            solver="internal",
            timeout_ms=1_000,
        )
        handler.assert_awaited_once_with(page, BLOCKING, "internal", 1_000)

    async def test_external_handler_must_be_configured(self):
        page = AsyncMock()
        page.evaluate.return_value = BLOCKING
        with self.assertRaises(RenderError) as raised:
            await handle_challenge(
                page,
                navigation_status=403,
                action="external",
                handler=None,
                solver=None,
                timeout_ms=1_000,
            )
        self.assertEqual(raised.exception.code, "captcha_handler_unavailable")

    async def test_external_handler_timeout_is_bounded(self):
        page = AsyncMock()
        page.evaluate.return_value = BLOCKING

        async def handler(*_args):
            await asyncio.sleep(1)

        with self.assertRaises(RenderError) as raised:
            await handle_challenge(
                page,
                navigation_status=403,
                action="external",
                handler=handler,
                solver=None,
                timeout_ms=1,
            )
        self.assertEqual(raised.exception.code, "captcha_handler_timeout")
