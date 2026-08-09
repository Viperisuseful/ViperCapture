import unittest

from vipercapture.page_cleanup import (
    CleanupOptions,
    apply_visual_cleanup,
    finish_autoconsent,
    setup_autoconsent,
    should_block_resource,
)


class CleanupPolicyTest(unittest.IsolatedAsyncioTestCase):
    def test_resource_blockers_are_explicit_and_independent(self):
        everything = CleanupOptions(
            block_ads=True,
            block_trackers=True,
            block_chats=True,
        )
        self.assertEqual(
            should_block_resource(
                "https://securepubads.g.doubleclick.net/tag.js", everything
            ),
            "ads",
        )
        self.assertEqual(
            should_block_resource(
                "https://www.google-analytics.com/g/collect", everything
            ),
            "trackers",
        )
        self.assertEqual(
            should_block_resource("https://widget.intercom.io/widget/app", everything),
            "chats",
        )
        self.assertIsNone(
            should_block_resource("https://notgoogle-analytics.com/page", everything)
        )
        self.assertIsNone(
            should_block_resource(
                "https://www.google-analytics.com/g/collect",
                CleanupOptions(block_ads=True),
            )
        )

    async def test_autoconsent_reject_uses_opt_out_rules_without_logging(self):
        class Page:
            def __init__(self):
                self.callback = None
                self.binding_name = None
                self.init_script = ""
                self.messages = []

            async def expose_function(self, name, callback):
                self.binding_name = name
                self.callback = callback

            async def add_init_script(self, script):
                self.init_script = script

            async def evaluate(self, script, argument=None):
                if argument:
                    self.messages.append(argument)
                return None

        page = Page()
        session = await setup_autoconsent(page, "reject")
        self.assertIsNotNone(session)
        self.assertIn("autoconsentReceiveMessage", page.init_script)
        self.assertNotEqual(page.binding_name, "autoconsentSendMessage")
        self.assertIn("if (calls >= 256) return", page.init_script)
        self.assertIn("Reflect.deleteProperty", page.init_script)
        await page.callback({"type": "init"})
        init = page.messages[-1]
        self.assertEqual(init["type"], "initResp")
        self.assertEqual(init["config"]["autoAction"], "optOut")
        self.assertFalse(any(init["config"]["logs"].values()))
        self.assertTrue(init["rules"]["autoconsent"])

        await page.callback({"type": "init"})
        self.assertEqual(len(page.messages), 1)

        await page.callback({"type": "autoconsentDone", "cmp": "test"})
        outcome = await finish_autoconsent(page, session)
        self.assertEqual(outcome["mode"], "reject")
        self.assertEqual(outcome["cmp"], "test")
        self.assertTrue(outcome["done"])

    async def test_visual_cleanup_uses_only_requested_categories(self):
        class Page:
            def __init__(self):
                self.css = ""
                self.scripts = []

            async def add_style_tag(self, *, content):
                self.css = content

            async def evaluate(self, script):
                self.scripts.append(script)

        page = Page()
        await apply_visual_cleanup(
            page,
            CleanupOptions(block_chats=True, block_newsletters=True),
        )
        self.assertIn("intercom-container", page.css)
        self.assertIn("newsletter-popup", page.css)
        self.assertNotIn("google_ads", page.css)
        self.assertIn("join our mailing list", page.scripts[-1])


if __name__ == "__main__":
    unittest.main()
