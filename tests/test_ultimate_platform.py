import asyncio
import io
import json
import os
import base64
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from vipercapture.compatibility import screenshotone_request, urlbox_request
from vipercapture.control_plane import (
    BaselineQuotaError,
    ControlPlane,
    Metrics,
    ProfileQuotaError,
    ScheduleQuotaError,
)
from vipercapture.render_contract import OutputFormat, RenderRequest
from vipercapture.render_engine import RenderArtifact, RenderLimits, diagnostic_bundle


class ControlPlaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.control = ControlPlane(Path(self.directory.name) / "control.sqlite3", encryption_secret="x" * 32)
        self.control.initialize()

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_keys_limits_ownership_profiles_and_audit(self):
        project = self.control.create_project("tests", 1, 1)
        self.assertEqual(
            self.control.list_projects()[0]["requests_per_minute"], 1
        )
        self.assertNotIn("rpm", self.control.list_projects()[0])
        key = self.control.create_key(str(project["id"]), "ci")
        identity = self.control.authenticate(key["api_key"])
        self.assertEqual(identity["project_id"], project["id"])
        with self.control._connect() as database:
            stored_digest = database.execute(
                "SELECT key_hash FROM api_keys WHERE id=?", (key["id"],)
            ).fetchone()[0]
        self.assertEqual(stored_digest, self.control._key_digest(key["api_key"]))
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
        self.assertTrue(
            self.control.is_owner(
                "profile", "profile", str(project["id"])
            )
        )
        raw_database = (Path(self.directory.name) / "control.sqlite3").read_bytes()
        self.assertNotIn(b"secret", raw_database)
        self.assertEqual(self.control.get_profile_any("profile"), state)
        self.control.audit(str(project["id"]), key["id"], "test", "j1")
        self.assertEqual(self.control.audits()[0]["action"], "test")
        self.assertTrue(self.control.revoke_key(key["id"]))
        self.assertIsNone(self.control.authenticate(key["api_key"]))

    async def test_read_only_requests_do_not_consume_render_concurrency(self):
        project = self.control.create_project("tests", 10, 1)
        project_id = str(project["id"])
        key = self.control.create_key(project_id, "ci")
        identity = self.control.authenticate(key["api_key"])
        self.assertTrue(await self.control.acquire_worker(project_id))
        allowed, reason = await self.control.acquire(
            identity, concurrency=False
        )
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        allowed, reason = await self.control.acquire(identity)
        self.assertFalse(allowed)
        self.assertEqual(reason, "concurrency_limit_exceeded")
        await self.control.release(project_id)

    async def test_resource_expiry_deletion_and_audit_retention(self):
        project = self.control.create_project("tests", 10, 1)
        project_id = str(project["id"])
        self.control.own("job", "expired", project_id, 60)
        with self.control._connect() as database:
            database.execute(
                "UPDATE resources SET expires_at=0 WHERE kind='job' AND id='expired'"
            )
        self.assertFalse(self.control.is_owner("job", "expired", project_id))

        self.control.put_profile(project_id, "profile", {}, None)
        self.control.own("profile", "profile", project_id)
        self.assertTrue(self.control.delete_profile(project_id, "profile"))
        self.assertFalse(self.control.is_owner("profile", "profile", project_id))

        with patch("vipercapture.control_plane.MAX_AUDIT_EVENTS", 3):
            for index in range(5):
                self.control.audit(project_id, "test", f"event-{index}")
        self.assertEqual(
            [event["action"] for event in reversed(self.control.audits(10))],
            ["event-2", "event-3", "event-4"],
        )

    async def test_schedule_reservations_are_bounded_and_recoverable(self):
        project = self.control.create_project("tests", 10, 1)
        project_id = str(project["id"])
        with patch("vipercapture.control_plane.MAX_SCHEDULES_PER_PROJECT", 1):
            self.control.reserve_schedule("one", project_id, 2)
            with self.assertRaises(ScheduleQuotaError):
                self.control.reserve_schedule("two", project_id, 1)
            self.assertTrue(
                self.control.disown("schedule", "one", project_id)
            )
            self.control.reserve_schedule("two", project_id, 1)
        with patch("vipercapture.control_plane.MAX_SCHEDULE_BYTES_PER_PROJECT", 2):
            with self.assertRaises(ScheduleQuotaError):
                self.control.resize_schedule("two", project_id, 3)

    async def test_quota_database_wait_does_not_block_event_loop(self):
        project = self.control.create_project("tests", 10, 1)
        key = self.control.create_key(str(project["id"]), "ci")
        identity = self.control.authenticate(key["api_key"])
        entered = threading.Event()
        release = threading.Event()
        original = self.control._acquire

        def slow_acquire(*args):
            entered.set()
            release.wait(timeout=1)
            return original(*args)

        with patch.object(self.control, "_acquire", slow_acquire):
            task = asyncio.create_task(self.control.acquire(identity))
            await asyncio.to_thread(entered.wait)
            started = asyncio.get_running_loop().time()
            await asyncio.sleep(0.02)
            elapsed = asyncio.get_running_loop().time() - started
            release.set()
            self.assertLess(elapsed, 0.1)
            self.assertTrue((await task)[0])
        await self.control.release(str(project["id"]))

    async def test_cancelled_quota_acquisition_releases_committed_lease(self):
        project = self.control.create_project("tests", 10, 1)
        project_id = str(project["id"])
        key = self.control.create_key(project_id, "ci")
        identity = self.control.authenticate(key["api_key"])
        entered = threading.Event()
        release = threading.Event()
        original = self.control._acquire

        def slow_acquire(*args):
            entered.set()
            release.wait(timeout=1)
            return original(*args)

        with patch.object(self.control, "_acquire", slow_acquire):
            task = asyncio.create_task(self.control.acquire(identity))
            await asyncio.to_thread(entered.wait)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with self.control._connect() as database:
            active = database.execute(
                "SELECT count(*) FROM active_leases WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
        self.assertEqual(active, 0)

    async def test_baseline_store(self):
        project = self.control.create_project("tests", 10, 1)
        document = self.control.put_baseline(str(project["id"]), "home", b"png")
        self.assertEqual(document["sha256"], "8f8cbb7dcf46e0bc7d53265749a6c17d116093a6ba95e442764060c76fd4a86c")
        self.assertEqual(self.control.get_baseline(str(project["id"]), "home"), b"png")
        self.assertTrue(self.control.delete_baseline(str(project["id"]), "home"))

    async def test_project_concurrency_is_atomic_across_control_instances(self):
        project = self.control.create_project("replicas", 10, 1)
        key = self.control.create_key(str(project["id"]), "ci")
        identity = self.control.authenticate(key["api_key"])
        replica = ControlPlane(
            Path(self.directory.name) / "control.sqlite3",
            encryption_secret="test-secret-that-is-at-least-32-bytes",
        )
        replica.initialize()
        allowed, _ = await self.control.acquire(identity)
        second, reason = await replica.acquire(identity)
        self.assertTrue(allowed)
        self.assertFalse(second)
        self.assertEqual(reason, "concurrency_limit_exceeded")
        await self.control.release(str(project["id"]))

    async def test_profile_save_preserves_expiration_and_baselines_are_bounded(self):
        project = self.control.create_project("tests", 10, 1)
        project_id = str(project["id"])
        self.control.put_profile(project_id, "profile", {}, 60)
        with self.control._connect() as database:
            expires_at = database.execute(
                "SELECT expires_at FROM profiles WHERE id='profile'"
            ).fetchone()[0]
        self.assertTrue(self.control.put_profile_any("profile", {"cookies": []}))
        with self.control._connect() as database:
            saved_expiration = database.execute(
                "SELECT expires_at FROM profiles WHERE id='profile'"
            ).fetchone()[0]
        self.assertEqual(saved_expiration, expires_at)
        with patch("vipercapture.control_plane.MAX_PROFILES_PER_PROJECT", 1):
            with self.assertRaises(ProfileQuotaError):
                self.control.put_profile(project_id, "second", {}, None)
        with patch("vipercapture.control_plane.MAX_BASELINES_PER_PROJECT", 1):
            self.control.put_baseline(project_id, "one", b"1")
            with self.assertRaises(BaselineQuotaError):
                self.control.put_baseline(project_id, "two", b"2")

    async def test_profile_delete_wins_a_concurrent_save(self):
        project = self.control.create_project("tests", 10, 1)
        project_id = str(project["id"])
        self.control.put_profile(project_id, "profile", {}, None)
        entered = threading.Event()
        release = threading.Event()
        original = self.control._check_profile_quota

        def pause_save(*args):
            entered.set()
            release.wait(timeout=2)
            return original(*args)

        with patch.object(self.control, "_check_profile_quota", pause_save):
            save = asyncio.create_task(
                asyncio.to_thread(
                    self.control.put_profile_any, "profile", {"cookies": []}
                )
            )
            await asyncio.to_thread(entered.wait)
            delete = asyncio.create_task(
                asyncio.to_thread(
                    self.control.delete_profile, project_id, "profile"
                )
            )
            await asyncio.sleep(0.02)
            release.set()
            self.assertTrue(await save)
            self.assertTrue(await delete)
        self.assertIsNone(self.control.get_profile(project_id, "profile"))

    @unittest.skipIf(os.name == "nt", "requires POSIX file mode semantics")
    async def test_control_database_files_are_owner_only(self):
        with self.control._connect() as database:
            database.execute(
                "INSERT INTO audit_events(project_id,actor,action,resource,created_at) "
                "VALUES (NULL,'test','permissions',NULL,0)"
            )
            paths = [
                Path(self.directory.name) / "control.sqlite3",
                Path(self.directory.name) / "control.sqlite3-wal",
                Path(self.directory.name) / "control.sqlite3-shm",
            ]
            for path in paths:
                if path.exists():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_rate_expiry_has_a_time_leading_index(self):
        with self.control._connect() as database:
            columns = database.execute(
                "PRAGMA index_info(rate_events_time)"
            ).fetchall()
        self.assertEqual([column[2] for column in columns], ["created_at"])

    async def test_resource_expiry_has_a_time_leading_index(self):
        with self.control._connect() as database:
            columns = database.execute(
                "PRAGMA index_info(resources_expiry)"
            ).fetchall()
        self.assertEqual([column[2] for column in columns], ["expires_at"])

    async def test_schedule_quota_has_a_project_leading_index(self):
        with self.control._connect() as database:
            columns = database.execute(
                "PRAGMA index_info(resources_schedule_project)"
            ).fetchall()
        self.assertEqual(
            [column[2] for column in columns], ["project_id", "id"]
        )


class CompatibilityTests(unittest.TestCase):
    def test_screenshotone_common_options(self):
        request = screenshotone_request({"url": "https://example.com", "format": "jpg", "full_page": "false", "viewport_width": "800"})
        self.assertEqual(request.output, OutputFormat.JPEG)
        self.assertEqual(request.viewport.width, 800)
        self.assertFalse(request.full_page)
        self.assertEqual(
            screenshotone_request(
                {"url": "https://example.com", "delay": "5"}
            ).wait_for.delay_ms,
            5000,
        )

    def test_urlbox_common_options(self):
        request = urlbox_request({"url": "https://example.com", "format": "jpg", "width": 900, "retina": True})
        self.assertEqual(request.output, OutputFormat.JPEG)
        self.assertEqual(request.viewport.device_scale_factor, 2)

    def test_unknown_vendor_option_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            screenshotone_request({"url": "https://example.com", "magic": "1"})

    def test_selector_adapters_disable_full_page_by_default(self):
        self.assertFalse(
            screenshotone_request(
                {"url": "https://example.com", "selector": "main"}
            ).full_page
        )
        self.assertFalse(
            urlbox_request(
                {"url": "https://example.com", "selector": "main"}
            ).full_page
        )


class ArtifactFeatureTests(unittest.IsolatedAsyncioTestCase):
    def test_new_format_contracts(self):
        self.assertEqual(RenderRequest(url="https://example.com", output="avif").output, OutputFormat.AVIF)
        self.assertIsNotNone(RenderRequest(url="https://example.com", output="mp4").video)
        with self.assertRaises(ValidationError):
            RenderRequest(url="https://example.com", output="pdf", slices={"height": 500})
        with self.assertRaises(ValidationError):
            RenderRequest(
                url="https://example.com",
                diagnostics={"bundle": True, "include_mhtml": True},
            )

    def test_browser_ui_exposes_every_render_output(self):
        source = (Path(__file__).parent.parent / "frontend" / "src" / "App.tsx").read_text()
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
        metrics.inc("renders_total", output='png"\\\n')
        self.assertIn('output="png\\"\\\\\\n"', metrics.prometheus())

    def test_integrations_use_safe_defaults(self):
        root = Path(__file__).parent.parent
        action = (root / "action.yml").read_text()
        terraform = (root / "integrations" / "terraform" / "main.tf").read_text()
        self.assertIn("http://localhost:8000/v1/render", action)
        self.assertNotIn('--url "${{ inputs.url }}"', action)
        self.assertIn("internal = 8000", terraform)
        self.assertIn("VIPERCAPTURE_CONTROL_SECRET", terraform)
        self.assertNotIn(";", terraform)
        self.assertIn('container_path = "/data"', terraform)
        workflow = json.loads(
            (root / "integrations" / "n8n-workflow.json").read_text()
        )
        node_types = {node["type"] for node in workflow["nodes"]}
        self.assertIn("n8n-nodes-base.manualTrigger", node_types)
        self.assertIn("n8n-nodes-base.set", node_types)
        self.assertTrue(workflow["connections"])
        self.assertIn("Pillow>=12.3.0", (root / "requirements.txt").read_text())
        self.assertIn("ffmpeg", (root / "Dockerfile").read_text())

    def test_worker_role_requires_async_jobs(self):
        result = subprocess.run(
            [sys.executable, "-c", "import vipercapture.main as main"],
            cwd=Path(__file__).parent.parent,
            env={
                **os.environ,
                "VIPERCAPTURE_ROLE": "worker",
                "VIPERCAPTURE_ASYNC_JOBS": "0",
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires VIPERCAPTURE_ASYNC_JOBS=1", result.stderr)


if __name__ == "__main__":
    unittest.main()
