import io
import json
import unittest
import zipfile

from pydantic import ValidationError

from render_contract import RenderRequest
from render_engine import (
    RenderArtifact,
    RenderLimits,
    diagnostic_bundle,
    diagnostic_url,
)


class DiagnosticsAndVideoTests(unittest.IsolatedAsyncioTestCase):
    def test_diagnostic_urls_drop_secrets(self):
        sanitized = diagnostic_url("https://user:pass@example.com/path?token=secret#fragment")
        self.assertEqual(sanitized, "https://example.com/path")

    async def test_diagnostic_bundle_is_self_describing(self):
        request = RenderRequest(
            html="<p>test</p>",
            diagnostics={"bundle": True},
        )
        artifact = RenderArtifact(
            b"image",
            "image/png",
            "capture.png",
            {"width": 1, "final_url": "https://example.com/path?token=secret"},
        )
        result = await diagnostic_bundle(
            artifact,
            request,
            [{"type": "log", "text": "ready"}],
            [{"method": "GET", "url": "https://example.com", "status": 200}],
            RenderLimits(),
        )
        self.assertEqual(result.media_type, "application/zip")
        with zipfile.ZipFile(io.BytesIO(result.body)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"capture.png", "manifest.json", "console.json", "network.json"},
            )
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["artifact"]["bytes"], 5)
            self.assertEqual(manifest["artifact"]["metadata"]["final_url"], "https://example.com/path")
            self.assertNotIn(b"secret", archive.read("manifest.json"))

    def test_video_contract_defaults_and_rejects_invalid_combinations(self):
        request = RenderRequest(url="https://example.com", output="webm")
        self.assertEqual(request.video.duration_ms, 5_000)
        self.assertEqual(request.credit_cost, 1)
        with self.assertRaises(ValidationError):
            RenderRequest(url="https://example.com", output="png", video={})
        with self.assertRaises(ValidationError):
            RenderRequest(
                url="https://example.com",
                output="webm",
                viewports=[
                    {"name": "one", "width": 10, "height": 10},
                    {"name": "two", "width": 10, "height": 10},
                ],
            )


if __name__ == "__main__":
    unittest.main()
