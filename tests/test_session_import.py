import json
import unittest

from vipercapture.render_errors import RenderError
from vipercapture.session_import import MAX_IMPORT_BYTES, import_storage_state


class SessionImportTests(unittest.TestCase):
    def test_imports_playwright_storage_state_with_local_storage(self):
        state = import_storage_state(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "session",
                            "value": "secret",
                            "domain": ".example.com",
                            "path": "/",
                            "expires": -1,
                            "httpOnly": True,
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    ],
                    "origins": [
                        {
                            "origin": "https://example.com",
                            "localStorage": [{"name": "theme", "value": "dark"}],
                        }
                    ],
                }
            ),
            format_name="playwright",
        )
        self.assertEqual(state["cookies"][0]["name"], "session")
        self.assertEqual(state["origins"][0]["localStorage"][0]["value"], "dark")

    def test_imports_common_browser_extension_cookie_json(self):
        state = import_storage_state(
            json.dumps(
                [
                    {
                        "name": "sid",
                        "value": "value",
                        "domain": "example.com",
                        "expirationDate": 2_000_000_000,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "no_restriction",
                    }
                ]
            )
        )
        self.assertEqual(state["cookies"][0]["sameSite"], "None")
        self.assertEqual(state["cookies"][0]["path"], "/")

    def test_imports_netscape_http_only_cookie(self):
        state = import_storage_state(
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t0\tsid\tvalue\n"
        )
        cookie = state["cookies"][0]
        self.assertTrue(cookie["httpOnly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["expires"], -1)

    def test_imports_pasted_cookie_header_for_an_origin(self):
        state = import_storage_state(
            "cookie: sid=abc; theme=dark",
            origin="https://example.com/account",
        )
        self.assertEqual([item["name"] for item in state["cookies"]], ["sid", "theme"])
        self.assertTrue(all(item["secure"] for item in state["cookies"]))

    def test_cookie_header_requires_an_origin(self):
        with self.assertRaisesRegex(ValueError, "origin is required"):
            import_storage_state("sid=abc")

    def test_rejects_oversized_imports(self):
        with self.assertRaises(RenderError) as raised:
            import_storage_state("x" * (MAX_IMPORT_BYTES + 1))
        self.assertEqual(raised.exception.code, "session_import_too_large")
