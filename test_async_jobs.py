import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from async_job_providers import LocalArtifactStore, SQLiteJobStore
from async_jobs import (
    Artifact,
    ArtifactStoreConfig,
    AsyncJobService,
    JobRecord,
    JobSettings,
    JobStoreConfig,
    PayloadCipher,
    RenderedArtifact,
    load_providers,
)
import main
from render_contract import RenderRequest
from render_errors import RenderError


UTC = timezone.utc


def _settings(path: Path, **changes) -> JobSettings:
    values = {
        "data_dir": path,
        "queue_limit": 2,
        "worker_count": 1,
        "queue_ttl": timedelta(minutes=15),
        "result_ttl": timedelta(minutes=5),
        "metadata_ttl": timedelta(hours=1),
        "max_attempts": 3,
        "poll_seconds": 0.01,
    }
    values.update(changes)
    return JobSettings(**values)


def _payload() -> RenderRequest:
    return RenderRequest.model_validate(
        {
            "url": "https://example.com/private-report",
            "headers": {"Authorization": "Bearer queued-secret"},
            "full_page": False,
        }
    )


async def _successful_renderer(_payload: RenderRequest) -> RenderedArtifact:
    return RenderedArtifact(
        body=b"\x89PNG\r\n\x1a\nasync-result",
        media_type="image/png",
        filename="capture.png",
        render_ms=12,
    )


class AsyncJobServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = _settings(self.root)
        self.store = SQLiteJobStore(
            JobStoreConfig(self.root, self.settings.metadata_ttl)
        )
        self.artifacts = LocalArtifactStore(
            ArtifactStoreConfig(self.root, self.settings.result_ttl)
        )
        self.secret = patch.dict(
            "os.environ",
            {"VIPERCAPTURE_JOB_SECRET": "test-only-job-secret"},
            clear=False,
        )
        self.secret.start()

    async def asyncTearDown(self):
        self.secret.stop()
        self.temporary.cleanup()

    async def test_job_is_encrypted_rendered_and_downloadable(self):
        service = AsyncJobService(
            self.settings,
            self.store,
            self.artifacts,
            _successful_renderer,
        )
        await service.start()
        try:
            job = await service.submit(_payload(), request_id="encrypted-job")
            for _ in range(100):
                current = await service.get(job.id)
                if current and current.status == "succeeded":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("job did not complete")

            self.assertIsNone(current.payload)
            self.assertEqual(current.attempt_count, 1)
            self.assertEqual(current.media_type, "image/png")
            artifact = await service.result(current)
            self.assertEqual(artifact.body, b"\x89PNG\r\n\x1a\nasync-result")

            with closing(sqlite3.connect(self.store.path)) as connection:
                row = connection.execute(
                    "SELECT payload FROM async_jobs WHERE id = ?",
                    (job.id,),
                ).fetchone()
            self.assertEqual(row, (None,))
            database = self.store.path.read_bytes()
            self.assertNotIn(b"private-report", database)
            self.assertNotIn(b"queued-secret", database)
        finally:
            await service.close()

    async def test_request_id_is_idempotent_and_queue_limit_is_atomic(self):
        await self.store.start()
        await self.artifacts.start()
        service = AsyncJobService(
            _settings(self.root, queue_limit=1),
            self.store,
            self.artifacts,
            _successful_renderer,
        )
        first = await service.submit(_payload(), request_id="same-request")
        repeated = await service.submit(_payload(), request_id="same-request")
        self.assertEqual(repeated.id, first.id)
        with self.assertRaises(RenderError) as error:
            await service.submit(_payload(), request_id="other-request")
        self.assertEqual(error.exception.code, "async_queue_full")
        cancelled = await service.cancel(first.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(cancelled.payload)
        await self.artifacts.close()
        await self.store.close()

    async def test_running_job_cannot_be_cancelled(self):
        rendering = asyncio.Event()
        release = asyncio.Event()

        async def blocked_renderer(_payload):
            rendering.set()
            await release.wait()
            return await _successful_renderer(_payload)

        service = AsyncJobService(
            self.settings,
            self.store,
            self.artifacts,
            blocked_renderer,
        )
        await service.start()
        try:
            job = await service.submit(_payload(), request_id="running-job")
            await asyncio.wait_for(rendering.wait(), timeout=1)
            with self.assertRaises(RenderError) as error:
                await service.cancel(job.id)
            self.assertEqual(error.exception.code, "job_already_running")
            release.set()
            for _ in range(100):
                current = await service.get(job.id)
                if current and current.status == "succeeded":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(current.status, "succeeded")
        finally:
            release.set()
            await service.close()

    async def test_running_job_requeues_after_restart(self):
        await self.store.start()
        await self.artifacts.start()
        service = AsyncJobService(
            self.settings,
            self.store,
            self.artifacts,
            _successful_renderer,
        )
        job = await service.submit(_payload(), request_id="restart-job")
        claimed = await self.store.claim(datetime.now(UTC))
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.status, "running")
        await self.artifacts.close()
        await self.store.close()

        recovered_store = SQLiteJobStore(
            JobStoreConfig(self.root, self.settings.metadata_ttl)
        )
        recovered_artifacts = LocalArtifactStore(
            ArtifactStoreConfig(self.root, self.settings.result_ttl)
        )
        recovered = AsyncJobService(
            self.settings,
            recovered_store,
            recovered_artifacts,
            _successful_renderer,
        )
        await recovered.start()
        try:
            for _ in range(100):
                current = await recovered.get(job.id)
                if current and current.status == "succeeded":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("requeued job did not complete")
            self.assertEqual(current.attempt_count, 2)
        finally:
            await recovered.close()

    async def test_restart_does_not_exceed_max_attempts(self):
        settings = _settings(self.root, max_attempts=1)
        await self.store.start()
        await self.artifacts.start()
        service = AsyncJobService(
            settings,
            self.store,
            self.artifacts,
            _successful_renderer,
        )
        job = await service.submit(_payload(), request_id="exhausted-restart")
        claimed = await self.store.claim(datetime.now(UTC))
        self.assertEqual(claimed.attempt_count, 1)
        await self.artifacts.close()
        await self.store.close()

        recovered_store = SQLiteJobStore(
            JobStoreConfig(self.root, settings.metadata_ttl)
        )
        recovered_artifacts = LocalArtifactStore(
            ArtifactStoreConfig(self.root, settings.result_ttl)
        )
        renderer = AsyncMock(side_effect=_successful_renderer)
        recovered = AsyncJobService(
            settings,
            recovered_store,
            recovered_artifacts,
            renderer,
        )
        await recovered.start()
        try:
            current = await recovered.get(job.id)
            self.assertEqual(current.status, "failed")
            self.assertEqual(current.attempt_count, 1)
            self.assertEqual(current.error_code, "job_attempts_exhausted")
            self.assertIsNone(current.payload)
            renderer.assert_not_awaited()
        finally:
            await recovered.close()

    async def test_shutdown_requeues_playwright_style_close_error(self):
        rendering = asyncio.Event()

        async def closing_renderer(_payload):
            rendering.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                raise RenderError(
                    "render_failed",
                    "The image render failed.",
                    500,
                    True,
                ) from exc

        service = AsyncJobService(
            self.settings,
            self.store,
            self.artifacts,
            closing_renderer,
        )
        await service.start()
        job = await service.submit(_payload(), request_id="shutdown-race")
        await asyncio.wait_for(rendering.wait(), timeout=1)
        await service.close()

        reopened = SQLiteJobStore(
            JobStoreConfig(self.root, self.settings.metadata_ttl)
        )
        await reopened.start()
        try:
            current = await reopened.get(job.id, datetime.now(UTC))
            self.assertEqual(current.status, "queued")
            self.assertIsNotNone(current.payload)
        finally:
            await reopened.close()

    async def test_retryable_render_error_retries_before_failing(self):
        calls = 0

        async def flaky_renderer(payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RenderError(
                    "render_failed",
                    "The image render failed.",
                    500,
                    True,
                )
            return await _successful_renderer(payload)

        settings = _settings(
            self.root,
            max_attempts=2,
            poll_seconds=0.001,
        )
        service = AsyncJobService(
            settings,
            self.store,
            self.artifacts,
            flaky_renderer,
        )
        await service.start()
        try:
            job = await service.submit(_payload(), request_id="retry-job")
            for _ in range(300):
                current = await service.get(job.id)
                if current and current.status == "succeeded":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("retryable job did not recover")
            self.assertEqual(current.attempt_count, 2)
            self.assertEqual(calls, 2)
        finally:
            await service.close()

    async def test_expired_artifacts_are_removed(self):
        await self.artifacts.start()
        expired = datetime.now(UTC) - timedelta(seconds=1)
        key = await self.artifacts.put(
            str(uuid4()),
            b"old",
            media_type="image/png",
            filename="old.png",
            expires_at=expired,
        )
        self.assertIsNone(await self.artifacts.get(key))
        data_path, metadata_path = self.artifacts._paths(key)
        self.assertFalse(data_path.exists())
        self.assertFalse(metadata_path.exists())
        await self.artifacts.close()

    async def test_metadata_cleanup_preserves_live_successful_result(self):
        settings = _settings(self.root, metadata_ttl=timedelta(seconds=1))
        store = SQLiteJobStore(
            JobStoreConfig(self.root, settings.metadata_ttl)
        )
        await store.start()
        try:
            now = datetime.now(UTC)
            completed_at = now - timedelta(seconds=2)
            job = JobRecord(
                id=str(uuid4()),
                request_id="live-result",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=completed_at,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=completed_at,
            )
            await store.create(job, active_limit=1)
            await store.claim(completed_at)
            await store.succeed(
                job.id,
                artifact_key="still-live",
                media_type="image/png",
                filename="capture.png",
                artifact_bytes=3,
                result_expires_at=now + timedelta(minutes=5),
                queue_ms=0,
                render_ms=1,
                now=completed_at,
            )

            expired_keys = await store.maintain(now)
            current = await store.get(job.id, now)
            self.assertNotIn("still-live", expired_keys)
            self.assertEqual(current.status, "succeeded")
            self.assertEqual(current.artifact_key, "still-live")
        finally:
            await store.close()

    async def test_cancelled_sqlite_call_keeps_lock_until_thread_finishes(self):
        await self.store.start()
        started = threading.Event()
        release = threading.Event()

        def blocked_operation(_connection):
            started.set()
            release.wait(timeout=2)

        operation = asyncio.create_task(self.store._run(blocked_operation))
        try:
            await asyncio.wait_for(
                asyncio.to_thread(started.wait),
                timeout=1,
            )
            operation.cancel()
            await asyncio.sleep(0)
            close = asyncio.create_task(self.store.close())
            await asyncio.sleep(0.02)
            self.assertFalse(close.done())

            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            await asyncio.wait_for(close, timeout=1)
        finally:
            release.set()
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            await self.store.close()

    async def test_worker_does_not_lose_wakeup_between_claim_and_wait(self):
        class InjectingStore(SQLiteJobStore):
            injected_job = None
            service = None

            async def claim(self, now):
                if self.injected_job is not None:
                    job, self.injected_job = self.injected_job, None
                    await self.create(job, active_limit=2)
                    self.service._wakeup.set()
                    return None
                return await super().claim(now)

        settings = _settings(self.root, poll_seconds=1)
        store = InjectingStore(
            JobStoreConfig(self.root, settings.metadata_ttl)
        )
        artifacts = LocalArtifactStore(
            ArtifactStoreConfig(self.root, settings.result_ttl)
        )
        rendered = asyncio.Event()

        async def renderer(payload):
            rendered.set()
            return await _successful_renderer(payload)

        service = AsyncJobService(settings, store, artifacts, renderer)
        now = datetime.now(UTC)
        job_id = str(uuid4())
        store.injected_job = JobRecord(
            id=job_id,
            request_id="claim-wakeup-race",
            status="queued",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=0,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
        )
        store.service = service
        await service.start()
        try:
            await asyncio.wait_for(rendered.wait(), timeout=0.25)
        finally:
            await service.close()


class ProviderLoadingTests(unittest.TestCase):
    def test_generated_local_key_is_private_and_restart_stable(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=False,
        ):
            os.environ.pop("VIPERCAPTURE_JOB_SECRET", None)
            root = Path(directory)
            first = PayloadCipher.for_data_dir(root)
            job_id = str(uuid4())
            encrypted = first.encrypt(job_id, _payload())
            second = PayloadCipher.for_data_dir(root)
            now = datetime.now(UTC)
            job = JobRecord(
                id=job_id,
                request_id="key-test",
                status="queued",
                payload=encrypted,
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=15),
                created_at=now,
            )
            self.assertEqual(
                str(second.decrypt(job).url),
                "https://example.com/private-report",
            )
            mode = (root / "async-jobs.key").stat().st_mode & 0o777
            self.assertEqual(mode & 0o077, 0)

    def test_external_factories_receive_provider_specific_config(self):
        module = ModuleType("test_async_provider_module")
        job_store = AsyncMock()
        artifact_store = AsyncMock()
        for provider, methods in (
            (
                job_store,
                (
                    "start", "close", "create", "claim", "get", "cancel",
                    "succeed", "fail", "requeue_running", "requeue", "maintain",
                ),
            ),
            (
                artifact_store,
                ("start", "close", "put", "get", "delete", "maintain"),
            ),
        ):
            for method in methods:
                setattr(provider, method, AsyncMock())
        received = {}

        def job_factory(config):
            received["job"] = config
            return job_store

        def artifact_factory(config):
            received["artifact"] = config
            return artifact_store

        module.job_factory = job_factory
        module.artifact_factory = artifact_factory
        sys.modules[module.__name__] = module
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                "os.environ",
                {
                    "VIPERCAPTURE_JOB_STORE_FACTORY":
                        f"{module.__name__}:job_factory",
                    "VIPERCAPTURE_ARTIFACT_STORE_FACTORY":
                        f"{module.__name__}:artifact_factory",
                },
                clear=False,
            ):
                settings = _settings(Path(directory))
                loaded_job, loaded_artifact = load_providers(settings)
            self.assertIs(loaded_job, job_store)
            self.assertIs(loaded_artifact, artifact_store)
            self.assertEqual(received["job"].data_dir, settings.data_dir)
            self.assertEqual(
                received["artifact"].result_ttl,
                settings.result_ttl,
            )
        finally:
            sys.modules.pop(module.__name__, None)


class AsyncJobRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = SimpleNamespace(
            submit=AsyncMock(),
            get=AsyncMock(),
            cancel=AsyncMock(),
            result=AsyncMock(),
        )
        main.app.state.async_jobs = self.service
        now = datetime.now(UTC)
        self.job = JobRecord(
            id=str(uuid4()),
            request_id="route-job",
            status="queued",
            payload=b"encrypted",
            attempt_count=0,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=15),
            created_at=now,
        )

    def tearDown(self):
        main.app.state.async_jobs = None

    async def test_submit_status_and_result_contract(self):
        self.service.submit.return_value = self.job
        response = await main.create_render_job(
            RenderRequest.model_validate(
                {"url": "https://example.com", "full_page": False}
            ),
            SimpleNamespace(state=SimpleNamespace(request_id="route-job")),
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["location"], f"/v1/jobs/{self.job.id}")
        self.assertEqual(json.loads(response.body)["status"], "queued")

        succeeded = self.job
        succeeded = succeeded.__class__(
            **{
                **succeeded.__dict__,
                "status": "succeeded",
                "payload": None,
                "artifact_key": "external-key",
                "media_type": "image/png",
                "filename": "capture.png",
                "artifact_bytes": 3,
                "result_expires_at": datetime.now(UTC) + timedelta(minutes=5),
                "completed_at": datetime.now(UTC),
            }
        )
        self.service.get.return_value = succeeded
        self.service.result.return_value = Artifact(
            "external-key",
            b"png",
            "image/png",
            "capture.png",
        )
        status = await main.read_render_job(self.job.id)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(json.loads(status.body)["result"]["bytes"], 3)
        result = await main.read_render_job_result(self.job.id)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.body, b"png")

    async def test_missing_job_raises_stable_error(self):
        self.service.get.return_value = None
        with self.assertRaisesRegex(Exception, "job_not_found"):
            await main.read_render_job(uuid4())

    async def test_expired_result_returns_gone(self):
        self.service.get.return_value = self.job.__class__(
            **{
                **self.job.__dict__,
                "status": "expired",
                "payload": None,
                "completed_at": datetime.now(UTC) - timedelta(minutes=1),
                "error_code": "async_result_expired",
                "error_message":
                    "The async job result is no longer available.",
                "error_retryable": False,
            }
        )
        with self.assertRaises(RenderError) as error:
            await main.read_render_job_result(self.job.id)
        self.assertEqual(error.exception.code, "async_result_expired")
        self.assertEqual(error.exception.status_code, 410)

    def test_openapi_exposes_complete_job_surface(self):
        paths = main.app.openapi()["paths"]
        self.assertIn("/v1/jobs", paths)
        self.assertIn("/v1/jobs/{job_id}", paths)
        self.assertIn("/v1/jobs/{job_id}/result", paths)
