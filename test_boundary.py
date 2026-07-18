"""Guard the intentionally limited public/private product boundary."""

from pathlib import Path
import subprocess
import unittest

from render_contract import RenderRequest


ROOT = Path(__file__).resolve().parent
FORBIDDEN = (
    "paypal", "referral_attributions", "render_credit_grants",
    "VIPERCAPTURE_DATABASE_URL", "/billing/", "/api/account",
    "autoconsent", "block_newsletters", "consent_mode",
)
SOURCE_SUFFIXES = {".py", ".js", ".html", ".css", ".sh", ".sql", ".toml", ".yml", ".yaml"}


class PublicBoundaryTest(unittest.TestCase):
    def test_tracked_executable_sources_have_no_private_symbols(self):
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8").split("\0")
        violations = []
        for relative in tracked:
            path = ROOT / relative
            if not relative or relative in {"README.md", "test_boundary.py"}:
                continue
            if path.suffix not in SOURCE_SUFFIXES and path.name != "Dockerfile":
                continue
            body = path.read_text(encoding="utf-8").lower()
            for symbol in FORBIDDEN:
                if symbol.lower() in body:
                    violations.append(f"{relative}: {symbol}")
        self.assertEqual(violations, [])

    def test_openapi_contract_is_url_to_image_only(self):
        schema = RenderRequest.model_json_schema()
        self.assertEqual(schema["required"], ["url"])
        self.assertNotIn("html", schema["properties"])
        self.assertNotIn("markdown", schema["properties"])
        self.assertEqual(
            schema["$defs"]["OutputFormat"]["enum"],
            ["png", "jpeg", "webp"],
        )

    def test_only_post_v1_render_is_exposed(self):
        from main import app

        render_routes = {
            route.path: set(route.methods or ())
            for route in app.routes
            if route.path in {"/v1/render", "/screenshot"}
        }
        self.assertEqual(render_routes, {"/v1/render": {"POST"}})


if __name__ == "__main__":
    unittest.main()
