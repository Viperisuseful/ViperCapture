import io
import json
import os
import base64
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from compatibility import screenshotone_request, urlbox_request
from control_plane import ControlPlane, Metrics
from render_contract import OutputFormat, RenderRequest
from render_engine import RenderArtifact, RenderLimits, diagnostic_bundle


class ControlPlaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.control = ControlPlane(Path(self.directory.name) / "control.sqlite3", encryption_secret="x" * 32)
        self.control.initialize()

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_keys_limits_ownership_profiles_and_audit(self):
        project = self.control.create_project("tests", 1, 1)
        key = self.control.create_key(str(project["id"]), "ci")
        identity = self.control.authenticate(key["api_key"])
        self.assertEqual(identity["project_id"], project["id"])
        with self.control._connect() as database:
            stored_digest = database.execute(
                "SELECT key_hash FROM api_keys WHERE id=?", (key["id"],)
            ).fetchone()[0]
        self.assertNotEqual(stored_digest, hashlib.sha256(key["api_key"].encode()).digest())
        allowed, _ = await self.control.acquire(identity)
        self.assertTrue(allowed)
        second, reason = await self.control.acquire(identity)
        self.assertFalse(second)
        self.assertEqual(reason, "rate_limit_exceeded")
        await self.control.release(str(project["id"]))
        self.control.own("job", "j1", str(project["id"]))
        self.assertTrue(self.control.is_owner("job", "j1", str(project["id"])))
        state = {"cookies": [{"name": "session", "value": "secret"}], "origins": []}
        self.control.put_profile(str(project["id"]), "profile", state, None)
        raw_database = (Path(self.directory.name) / "control.sqlite3").read_bytes()
        self.assertNotIn(b"secret", raw_database)
        self.assertEqual(self.control.get_profile_any("profile"), state)
        self.control.audit(str(project["id"]), key["id"], "test", "j1")
        self.assertEqual(self.control.audits()[0]["action"], "test")
        self.assertTrue(self.control.revoke_key(key["id"]))
        self.assertIsNone(self.control.authenticate(key["api_key"]))

    async def test_baseline_store(self):
        project = self.control.create_project("tests", 10, 1)
        document = self.control.put_baseline(str(project["id"]), "home", b"png")
        self.assertEqual(document["sha256"], "8f8cbb7dcf46e0bc7d53265749a6c17d116093a6ba95e442764060c76fd4a86c")
        self.assertEqual(self.control.get_baseline(str(project["id"]), "home"), b"png")


class CompatibilityTests(unittest.TestCase):
    def test_screenshotone_common_options(self):
        request = screenshotone_request({"url": "https://example.com", "format": "jpg", "full_page": "false", "viewport_width": "800"})
        self.assertEqual(request.output, OutputFormat.JPEG)
        self.assertEqual(request.viewport.width, 800)
        self.assertFalse(request.full_page)

    def test_urlbox_common_options(self):
        request = urlbox_request({"url": "https://example.com", "format": "jpg", "width": 900, "retina": True})
        self.assertEqual(request.output, OutputFormat.JPEG)
        self.assertEqual(request.viewport.device_scale_factor, 2)

    def test_unknown_vendor_option_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            screenshotone_request({"url": "https://example.com", "magic": "1"})


class ArtifactFeatureTests(unittest.IsolatedAsyncioTestCase):
    def test_new_format_contracts(self):
        self.assertEqual(RenderRequest(url="https://example.com", output="avif").output, OutputFormat.AVIF)
        self.assertIsNotNone(RenderRequest(url="https://example.com", output="mp4").video)
        with self.assertRaises(ValidationError):
            RenderRequest(url="https://example.com", output="pdf", slices={"height": 500})

    def test_browser_ui_exposes_every_render_output(self):
        source = (Path(__file__).parent / "frontend" / "src" / "App.tsx").read_text()
        for output in OutputFormat:
            self.assertIn(f'value="{output.value}"', source)

    async def test_slices_and_certification_are_verifiable_archives(self):
        image = Image.new("RGB", (20, 250), "white")
        body = io.BytesIO()
        image.save(body, "PNG")
        request = RenderRequest(
            url="https://example.com",
            output="png",
            slices={"height": 100, "overlap": 10},
            certification={"enabled": True},
        )
        with patch.dict(os.environ, {"VIPERCAPTURE_CERTIFICATION_SECRET": "c" * 32}):
            result = await diagnostic_bundle(
                RenderArtifact(body.getvalue(), "image/png", "capture.png"),
                request,
                [],
                [],
                RenderLimits(),
            )
        self.assertEqual(result.filename, "vipercapture-certified.zip")
        with zipfile.ZipFile(io.BytesIO(result.body)) as archive:
            manifest_body = archive.read("manifest.json").rstrip(b"\n")
            manifest = json.loads(manifest_body)
            self.assertEqual(manifest["algorithm"], "Ed25519")
            self.assertIn("manifest.sig", archive.namelist())
            public_key = base64.urlsafe_b64decode(manifest["public_key"] + "==")
            signature = base64.urlsafe_b64decode(archive.read("manifest.sig").strip() + b"==")
            Ed25519PublicKey.from_public_bytes(public_key).verify(signature, manifest_body)
            nested = archive.read("vipercapture-slices.zip")
        with zipfile.ZipFile(io.BytesIO(nested)) as slices:
            self.assertEqual(len([name for name in slices.namelist() if name.startswith("slices/")]), 3)

    def test_prometheus_metrics(self):
        metrics = Metrics()
        metrics.inc("renders_total", output="png")
        self.assertIn('vipercapture_renders_total{output="png"} 1', metrics.prometheus())


if __name__ == "__main__":
    unittest.main()
