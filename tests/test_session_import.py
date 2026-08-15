import json
import unittest

from vipercapture.render_errors import RenderError
from vipercapture.session_import import MAX_IMPORT_BYTES, import_storage_state


class SessionImportTests(unittest.TestCase):
    def test_imports_utf8_bom_exports(self):
        cookie_json = '\ufeff[{"name":"sid","value":"value","domain":"example.com"}]'
        playwright = '\ufeff{"cookies":[],"origins":[]}'

        imported_cookies = import_storage_state(cookie_json)
        imported_playwright = import_storage_state(
            playwright,
            format_name="playwright",
        )

        self.assertEqual(imported_cookies["cookies"][0]["name"], "sid")
        self.assertEqual(imported_playwright, {"cookies": [], "origins": []})

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
                            "partitionKey": "https://top-level.example",
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
        self.assertEqual(
            state["cookies"][0]["partitionKey"], "https://top-level.example"
        )
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

    def test_netscape_preserves_empty_values_and_subdomain_scope(self):
        state = import_storage_state(
            "example.com\tTRUE\t/\tFALSE\t0\tempty\t\n",
            format_name="netscape",
        )
        cookie = state["cookies"][0]
        self.assertEqual(cookie["domain"], ".example.com")
        self.assertEqual(cookie["value"], "")

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

    def test_malformed_cookie_header_is_a_validation_error(self):
        with self.assertRaisesRegex(ValueError, "malformed"):
            import_storage_state(
                "sid=ok; bad@name=x",
                format_name="cookie_header",
                origin="https://example.com",
            )

    def test_json_formats_require_their_document_shapes(self):
        with self.assertRaisesRegex(ValueError, "requires cookie and origin arrays"):
            import_storage_state('{"foo":"bar"}')
        with self.assertRaisesRegex(TypeError, "must be a JSON array"):
            import_storage_state(
                '{"cookies":[],"origins":[]}', format_name="cookies_json"
            )

    def test_rejects_structured_partition_keys_instead_of_widening_scope(self):
        with self.assertRaisesRegex(ValueError, "partitionKey"):
            import_storage_state(
                json.dumps(
                    [
                        {
                            "name": "sid",
                            "value": "secret",
                            "domain": "example.com",
                            "partitionKey": {"topLevelSite": "https://example.org"},
                        }
                    ]
                )
            )

    def test_rejects_oversized_imports(self):
        with self.assertRaises(RenderError) as raised:
            import_storage_state("x" * (MAX_IMPORT_BYTES + 1))
        self.assertEqual(raised.exception.code, "session_import_too_large")
