import unittest
from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError

from render_contract import RenderRequest
from render_engine import RenderEngine, RenderLimits
from render_errors import RenderError


def action_page():
    page = MagicMock()
    locator = MagicMock()
    locator.first = locator
    locator.click = AsyncMock()
    locator.hover = AsyncMock()
    locator.fill = AsyncMock()
    locator.press = AsyncMock()
    locator.select_option = AsyncMock()
    locator.scroll_into_view_if_needed = AsyncMock()
    locator.wait_for = AsyncMock()
    locator.evaluate_all = AsyncMock()
    page.locator.return_value = locator
    page.evaluate = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    return page, locator


class ActionContractTests(unittest.TestCase):
    def test_typed_actions_and_network_controls_validate(self):
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "actions": [
                    {"type": "click", "selector": "#open"},
                    {"type": "fill", "selector": "#name", "value": "Viper"},
                    {"type": "press", "key": "Enter"},
                    {"type": "select", "selector": "select", "values": ["one"]},
                    {"type": "scroll", "y": 800},
                    {"type": "wait", "value": "Ready"},
                    {"type": "hide", "selector": ".overlay"},
                ],
                "network": {
                    "user_agent": "ViperCapture test",
                    "geolocation": {"latitude": 51.5, "longitude": -0.1},
                    "proxy": {"server": "socks5://proxy.example:1080"},
                    "cookies": [
                        {"name": "theme", "value": "dark", "domain": "example.com"}
                    ],
                    "block_url_patterns": ["*analytics*"],
                    "block_resource_types": ["media", "font"],
                    "bypass_csp": True,
                },
            }
        )
        self.assertEqual(len(request.actions), 7)
        self.assertEqual(request.network.proxy.server, "socks5://proxy.example:1080")

    def test_invalid_action_and_proxy_are_rejected(self):
        with self.assertRaises(ValidationError):
            RenderRequest.model_validate(
                {"url": "https://example.com", "actions": [{"type": "click"}]}
            )
        with self.assertRaises(ValidationError):
            RenderRequest.model_validate(
                {
                    "url": "https://example.com",
                    "network": {"proxy": {"server": "file:///tmp/proxy"}},
                }
            )


class ActionExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_actions_execute_in_order(self):
        page, locator = action_page()
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "actions": [
                    {"type": "click", "selector": "#open"},
                    {"type": "fill", "selector": "#name", "value": "Viper"},
                    {"type": "press", "selector": "#name", "key": "Enter"},
                    {"type": "select", "selector": "select", "values": ["one"]},
                    {"type": "scroll", "y": 400},
                    {"type": "wait", "selector": "#ready", "delay_ms": 5},
                    {"type": "hide", "selector": ".overlay"},
                ],
            }
        )
        await RenderEngine(hosted=False)._run_actions(page, request, RenderLimits())
        locator.click.assert_awaited_once()
        locator.fill.assert_awaited_once_with("Viper", timeout=15_000)
        locator.press.assert_awaited_once_with("Enter", timeout=15_000)
        locator.select_option.assert_awaited_once_with(["one"], timeout=15_000)
        locator.scroll_into_view_if_needed.assert_not_awaited()
        locator.wait_for.assert_awaited_once_with(state="visible", timeout=15_000)
        locator.evaluate_all.assert_awaited_once()

    async def test_javascript_requires_operator_opt_in(self):
        page, _locator = action_page()
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "actions": [{"type": "javascript", "value": "() => 42"}],
            }
        )
        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False)._run_actions(page, request, RenderLimits())
        self.assertEqual(raised.exception.code, "scripts_disabled")

        await RenderEngine(hosted=False, allow_scripts=True)._run_actions(
            page, request, RenderLimits()
        )
        page.evaluate.assert_awaited_once_with("() => 42")

    async def test_hide_uses_the_complete_locator(self):
        page = MagicMock()
        locator = MagicMock()
        locator.first = MagicMock()
        locator.evaluate_all = AsyncMock()
        locator.first.evaluate_all = AsyncMock()
        page.locator.return_value = locator
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "actions": [{"type": "hide", "selector": ".overlay"}],
            }
        )
        await RenderEngine(hosted=False)._run_actions(
            page,
            request,
            RenderLimits(),
        )
        locator.evaluate_all.assert_awaited_once()
        locator.first.evaluate_all.assert_not_awaited()

    async def test_content_and_request_assertions_fail_closed(self):
        page, _locator = action_page()
        page.evaluate.side_effect = [False]
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "assertions": {"content_includes": ["Ready"]},
            }
        )
        with self.assertRaises(RenderError) as raised:
            await RenderEngine(hosted=False)._check_assertions(page, request, [])
        self.assertEqual(raised.exception.code, "content_assertion_failed")
