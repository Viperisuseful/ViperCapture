import asyncio
import errno
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import main
from async_job_providers import LocalArtifactStore, SQLiteJobStore
from async_jobs import (
    Artifact,
    ArtifactStoreConfig,
    AsyncJobService,
    JobConflictError,
    JobDeferred,
    JobRecord,
    JobSettings,
    JobStoreConfig,
    PayloadCipher,
    RenderedArtifact,
    StoredArtifact,
    load_providers,
    public_job_document,
    settings_from_environment,
)
from render_contract import RenderRequest
from render_errors import RenderError
from webhooks import WebhookDeliveryError

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

    async def test_submit_rejects_webhook_without_notifier(self):
        service = AsyncJobService(
            self.settings,
            self.store,
            self.artifacts,
            _successful_renderer,
        )
        payload = RenderRequest.model_validate(
            {
                "url": "https://example.com/report",
                "delivery": {"webhook_url": "https://hooks.example/callback"},
            }
        )
        with self.assertRaises(RenderError) as raised:
            await service.submit(payload, request_id="disabled-webhook")
        self.assertEqual(raised.exception.code, "webhooks_disabled")
        self.assertFalse(raised.exception.retryable)
        self.assertFalse(self.store.path.exists())

    async def test_api_only_service_does_not_recover_distributed_jobs(self):
        job_store = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(),
            requeue_running=AsyncMock(),
            maintain=AsyncMock(return_value=[]),
        )
        artifact_store = SimpleNamespace(
            start=AsyncMock(), close=AsyncMock(), maintain=AsyncMock()
        )
        service = AsyncJobService(
            _settings(self.root, worker_count=0),
            job_store,
            artifact_store,
            _successful_renderer,
            cipher=PayloadCipher(b"\0" * 32),
            recover_running=False,
        )
        await service.start()
        try:
            job_store.requeue_running.assert_not_awaited()
        finally:
            await service.close()

    async def test_split_worker_recovers_only_stale_leased_claims(self):
        job_store = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(),
            recover_stale=AsyncMock(),
            requeue_running=AsyncMock(),
            maintain=AsyncMock(return_value=[]),
        )
        artifact_store = SimpleNamespace(
            start=AsyncMock(), close=AsyncMock(), maintain=AsyncMock()
        )
        service = AsyncJobService(
            _settings(self.root, worker_count=0),
            job_store,
            artifact_store,
            _successful_renderer,
            cipher=PayloadCipher(b"\0" * 32),
            recover_running=False,
            recover_stale=True,
        )
        await service.start()
        try:
            job_store.recover_stale.assert_awaited_once()
            job_store.requeue_running.assert_not_awaited()
        finally:
            await service.close()

    def test_zero_workers_requires_explicit_api_role_opt_in(self):
        with patch.dict("os.environ", {"VIPERCAPTURE_JOB_WORKERS": "0"}):
            with self.assertRaisesRegex(ValueError, "must be positive"):
                settings_from_environment(base_dir=self.root)
            settings = settings_from_environment(
                base_dir=self.root, allow_zero_workers=True
            )
        self.assertEqual(settings.worker_count, 0)

    def test_public_job_document_hides_project_namespace(self):
        now = datetime.now(UTC)
        job = JobRecord(
            id=str(uuid4()),
            request_id=f"_project-{'a' * 24}:caller-visible",
            status="queued",
            payload=b"encrypted",
            attempt_count=0,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
        )
        self.assertEqual(
            public_job_document(job)["request_id"], "caller-visible"
        )

    async def test_deferred_claim_does_not_spend_a_render_attempt(self):
        calls = 0

        async def renderer(payload):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise JobDeferred(0.01)
            return await _successful_renderer(payload)

        service = AsyncJobService(
            self.settings,
            self.store,
            self.artifacts,
            renderer,
        )
        await service.start()
        try:
            job = await service.submit(_payload(), request_id="deferred-job")
            for _ in range(100):
                current = await service.get(job.id)
                if current and current.status == "succeeded":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("deferred job did not complete")
            self.assertEqual(calls, 3)
            self.assertEqual(current.attempt_count, 1)
        finally:
            await service.close()

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

    async def test_webhook_outbox_survives_restart_and_clears_after_delivery(self):
        failing_notifier = AsyncMock(side_effect=RuntimeError("offline"))
        service = AsyncJobService(
            self.settings,
            self.store,
            self.artifacts,
            _successful_renderer,
            notifier=failing_notifier,
        )
        await service.start()
        payload = RenderRequest.model_validate(
            {
                "url": "https://example.com/report",
                "delivery": {"webhook_url": "https://hooks.example/callback"},
            }
        )
        job = await service.submit(payload, request_id="durable-webhook")
        for _ in range(100):
            current = await service.get(job.id)
            if current and current.status == "succeeded":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(current.status, "succeeded")
        self.assertIsNone(current.payload)
        pending = await self.store.pending_notifications(
            datetime.now(UTC) + timedelta(seconds=1)
        )
        self.assertEqual([item.id for item in pending], [job.id])
        self.assertNotIn(b"hooks.example", self.store.path.read_bytes())
        await service.close()

        recovered_store = SQLiteJobStore(
            JobStoreConfig(self.root, self.settings.metadata_ttl)
        )
        recovered_artifacts = LocalArtifactStore(
            ArtifactStoreConfig(self.root, self.settings.result_ttl)
        )
        delivered = AsyncMock()
        recovered = AsyncJobService(
            self.settings,
            recovered_store,
            recovered_artifacts,
            _successful_renderer,
            notifier=delivered,
        )
        await recovered.start()
        try:
            for _ in range(100):
                if delivered.await_count:
                    break
                await asyncio.sleep(0.01)
            delivered.assert_awaited_once()
            url, event_job = delivered.await_args.args
            self.assertEqual(url, "https://hooks.example/callback")
            self.assertEqual(event_job.status, "succeeded")
            self.assertEqual(
                await recovered_store.pending_notifications(datetime.now(UTC)),
                [],
            )
        finally:
            await recovered.close()

    async def test_slow_webhook_does_not_block_render_workers(self):
        notifier_started = asyncio.Event()
        release_notifier = asyncio.Event()

        async def slow_notifier(_url, _job):
            notifier_started.set()
            await release_notifier.wait()

        service = AsyncJobService(
            self.settings,
            self.store,
            self.artifacts,
            _successful_renderer,
            notifier=slow_notifier,
        )
        await service.start()
        webhook_payload = RenderRequest.model_validate(
            {
                "url": "https://example.com/first",
                "delivery": {"webhook_url": "https://hooks.example/slow"},
            }
        )
        try:
            await service.submit(webhook_payload, request_id="slow-webhook")
            await asyncio.wait_for(notifier_started.wait(), timeout=1)
            second = await service.submit(
                RenderRequest(url="https://example.com/second"),
                request_id="render-while-webhook-slow",
            )
            for _ in range(100):
                current = await service.get(second.id)
                if current and current.status == "succeeded":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(current.status, "succeeded")
        finally:
            release_notifier.set()
            await service.close()

    async def test_failed_webhook_backoff_allows_later_delivery(self):
        first_attempted = asyncio.Event()
        second_delivered = asyncio.Event()

        async def notifier(url, _job):
            if url.endswith("/failing"):
                first_attempted.set()
                raise RuntimeError("unavailable")
            second_delivered.set()

        service = AsyncJobService(
            self.settings,
            self.store,
            self.artifacts,
            _successful_renderer,
            notifier=notifier,
        )
        await service.start()
        try:
            await service.submit(
                RenderRequest.model_validate(
                    {
                        "url": "https://example.com/first",
                        "delivery": {
                            "webhook_url": "https://hooks.example/failing"
                        },
                    }
                ),
                request_id="failing-webhook",
            )
            await asyncio.wait_for(first_attempted.wait(), timeout=1)
            await service.submit(
                RenderRequest.model_validate(
                    {
                        "url": "https://example.com/second",
                        "delivery": {
                            "webhook_url": "https://hooks.example/working"
                        },
                    }
                ),
                request_id="working-webhook",
            )
            await asyncio.wait_for(second_delivered.wait(), timeout=1)
        finally:
            await service.close()

    async def test_slow_webhook_does_not_block_later_outbox_rows(self):
        store = SimpleNamespace(
            pending_notifications=AsyncMock(),
            acknowledge_notification=AsyncMock(),
            defer_notification=AsyncMock(),
        )
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        healthy_delivered = asyncio.Event()

        async def notifier(url, _job):
            if url.endswith("/slow"):
                slow_started.set()
                await release_slow.wait()
            else:
                healthy_delivered.set()

        service = AsyncJobService(
            self.settings,
            store,
            self.artifacts,
            _successful_renderer,
            notifier=notifier,
        )
        now = datetime.now(UTC)

        def pending_job(name):
            job_id = str(uuid4())
            return JobRecord(
                id=job_id,
                request_id=f"outbox-{name}",
                status="succeeded",
                payload=None,
                attempt_count=1,
                available_at=now,
                queue_expires_at=now,
                created_at=now,
                webhook_payload=service.cipher.encrypt_webhook(
                    job_id, f"https://hooks.example/{name}"
                ),
                webhook_event_status="succeeded",
            )

        store.pending_notifications.return_value = [
            pending_job("slow"),
            pending_job("healthy"),
        ]
        operation = asyncio.create_task(service._drain_notifications())
        try:
            await asyncio.wait_for(slow_started.wait(), timeout=1)
            await asyncio.wait_for(healthy_delivered.wait(), timeout=1)
        finally:
            release_slow.set()
            await operation

    async def test_webhook_is_dead_lettered_after_tenth_failure(self):
        store = SimpleNamespace(
            pending_notifications=AsyncMock(),
            acknowledge_notification=AsyncMock(),
            defer_notification=AsyncMock(),
        )
        notifier = AsyncMock(side_effect=RuntimeError("unavailable"))
        service = AsyncJobService(
            self.settings,
            store,
            self.artifacts,
            _successful_renderer,
            notifier=notifier,
        )
        now = datetime.now(timezone.utc)
        job_id = str(uuid4())
        webhook_payload = service.cipher.encrypt_webhook(
            job_id, "https://hooks.example/failing"
        )
        job = JobRecord(
            id=job_id,
            request_id="dead-letter-webhook",
            status="succeeded",
            payload=None,
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            webhook_payload=webhook_payload,
            webhook_event_status="succeeded",
            webhook_attempt_count=9,
        )
        store.pending_notifications.return_value = [job]

        await service._drain_notifications()

        notifier.assert_awaited_once()
        store.acknowledge_notification.assert_awaited_once_with(
            job_id, webhook_payload
        )
        store.defer_notification.assert_not_awaited()

    async def test_permanent_webhook_failure_is_acknowledged_immediately(self):
        store = SimpleNamespace(
            pending_notifications=AsyncMock(),
            acknowledge_notification=AsyncMock(),
            defer_notification=AsyncMock(),
        )
        notifier = AsyncMock(
            side_effect=WebhookDeliveryError("HTTP 400", retryable=False)
        )
        service = AsyncJobService(
            self.settings,
            store,
            self.artifacts,
            _successful_renderer,
            notifier=notifier,
        )
        now = datetime.now(UTC)
        job_id = str(uuid4())
        webhook_payload = service.cipher.encrypt_webhook(
            job_id, "https://hooks.example/permanent"
        )
        store.pending_notifications.return_value = [
            JobRecord(
                id=job_id,
                request_id="permanent-webhook",
                status="failed",
                payload=None,
                attempt_count=1,
                available_at=now,
                queue_expires_at=now,
                created_at=now,
                webhook_payload=webhook_payload,
                webhook_event_status="failed",
            )
        ]

        await service._drain_notifications()

        store.acknowledge_notification.assert_awaited_once_with(
            job_id, webhook_payload
        )
        store.defer_notification.assert_not_awaited()

    async def test_delayed_success_webhook_clears_later_expiry_error(self):
        store = SimpleNamespace(
            pending_notifications=AsyncMock(),
            acknowledge_notification=AsyncMock(),
            defer_notification=AsyncMock(),
        )
        delivered = []

        async def notifier(_url, job):
            delivered.append(job)

        service = AsyncJobService(
            self.settings,
            store,
            self.artifacts,
            _successful_renderer,
            notifier=notifier,
        )
        now = datetime.now(timezone.utc)
        job_id = str(uuid4())
        webhook_payload = service.cipher.encrypt_webhook(
            job_id, "https://hooks.example/success"
        )
        store.pending_notifications.return_value = [
            JobRecord(
                id=job_id,
                request_id="delayed-success",
                status="expired",
                payload=None,
                attempt_count=1,
                available_at=now,
                queue_expires_at=now,
                created_at=now,
                webhook_payload=webhook_payload,
                webhook_event_status="succeeded",
                error_code="async_result_expired",
                error_message="expired",
                error_retryable=False,
                media_type="image/png",
                filename="capture.png",
                artifact_bytes=4,
            )
        ]

        await service._drain_notifications()

        self.assertEqual(delivered[0].status, "succeeded")
        self.assertIsNone(delivered[0].error_code)
        self.assertIsNone(delivered[0].error_message)
        self.assertIsNone(delivered[0].error_retryable)

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
        different = RenderRequest.model_validate(
            {
                "url": "https://example.com/different-report",
                "full_page": False,
            }
        )
        with self.assertRaises(RenderError) as conflict:
            await service.submit(different, request_id="same-request")
        self.assertEqual(
            conflict.exception.code,
            "idempotency_key_conflict",
        )
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
        claimed = await self.store.claim(
            datetime.now(UTC),
            self.settings.max_attempts,
        )
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

    async def test_stale_requeue_cannot_replace_a_newer_running_attempt(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="stale-requeue",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            first = await self.store.claim(now, self.settings.max_attempts)
            self.assertEqual(first.attempt_count, 1)

            await self.store.requeue(job.id, first.attempt_count, now)
            second = await self.store.claim(now, self.settings.max_attempts)
            self.assertEqual(second.attempt_count, 2)

            await self.store.requeue(
                job.id,
                first.attempt_count,
                now + timedelta(seconds=1),
            )
            current = await self.store.get(job.id, now)
            self.assertEqual(current.status, "running")
            self.assertEqual(current.attempt_count, 2)
        finally:
            await self.store.close()

    async def test_retry_expiration_uses_transition_time(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="retry-expiration-time",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(milliseconds=10),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            claimed = await self.store.claim(now, self.settings.max_attempts)
            future_retry = now + timedelta(minutes=1)

            before = datetime.now(UTC)
            await self.store.requeue(
                job.id,
                claimed.attempt_count,
                future_retry,
            )
            current = await self.store.get(job.id, future_retry)

            self.assertEqual(current.status, "expired")
            self.assertGreaterEqual(current.completed_at, before)
            self.assertLess(current.completed_at, future_retry)
        finally:
            await self.store.close()

    async def test_get_normalizes_queued_and_result_expiration(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            queued = JobRecord(
                id=str(uuid4()),
                request_id="expired-queued-status",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now - timedelta(minutes=1),
                queue_expires_at=now - timedelta(seconds=1),
                created_at=now - timedelta(minutes=1),
            )
            await self.store.create(queued, active_limit=2)
            expired_queue = await self.store.get(queued.id, now)
            self.assertEqual(expired_queue.status, "expired")
            self.assertEqual(expired_queue.error_code, "job_queue_expired")
            self.assertIsNone(expired_queue.payload)

            successful = JobRecord(
                id=str(uuid4()),
                request_id="expired-result-status",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now - timedelta(minutes=1),
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now - timedelta(minutes=1),
            )
            await self.store.create(successful, active_limit=2)
            await self.store.claim(now, self.settings.max_attempts)
            await self.store.succeed(
                successful.id,
                expected_attempt=1,
                artifact_key="expired-result",
                media_type="image/png",
                filename="capture.png",
                artifact_bytes=3,
                result_expires_at=now + timedelta(seconds=1),
                queue_ms=0,
            )
            expired_result = await self.store.get(
                successful.id,
                now + timedelta(seconds=2),
            )
            self.assertEqual(expired_result.status, "expired")
            self.assertEqual(
                expired_result.error_code,
                "async_result_expired",
            )
            self.assertEqual(expired_result.artifact_key, "expired-result")
        finally:
            await self.store.close()

    async def test_get_scrubs_only_when_queued_payload_expires(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="conditional-status-scrub",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(seconds=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)

            with patch.object(
                SQLiteJobStore,
                "_scrub_payload_history",
                wraps=SQLiteJobStore._scrub_payload_history,
            ) as scrub:
                current = await self.store.get(job.id, now)
                self.assertEqual(current.status, "queued")
                scrub.assert_not_called()

                expired = await self.store.get(
                    job.id,
                    now + timedelta(seconds=2),
                )
                self.assertEqual(expired.status, "expired")
                scrub.assert_called_once()

                repeated = await self.store.get(
                    job.id,
                    now + timedelta(seconds=3),
                )
                self.assertEqual(repeated.status, "expired")
                scrub.assert_called_once()
        finally:
            await self.store.close()

    async def test_missing_artifact_expires_successful_job(self):
        await self.store.start()
        await self.artifacts.start()
        service = AsyncJobService(
            self.settings,
            self.store,
            self.artifacts,
            _successful_renderer,
        )
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="missing-successful-artifact",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            claimed = await self.store.claim(
                now,
                self.settings.max_attempts,
            )
            artifact_key = f"{uuid4()}.png"
            await self.store.succeed(
                claimed.id,
                expected_attempt=claimed.attempt_count,
                artifact_key=artifact_key,
                media_type="image/png",
                filename="missing.png",
                artifact_bytes=3,
                result_expires_at=now + timedelta(minutes=1),
                queue_ms=0,
            )
            successful = await self.store.get(job.id, now)
            self.assertEqual(successful.status, "succeeded")

            self.assertIsNone(await service.result(successful))

            expired = await self.store.get(job.id, datetime.now(UTC))
            self.assertEqual(expired.status, "expired")
            self.assertEqual(expired.error_code, "async_result_expired")
            self.assertEqual(expired.artifact_key, artifact_key)
        finally:
            await self.artifacts.close()
            await self.store.close()

    async def test_result_expiring_during_fetch_is_not_returned(self):
        await self.store.start()
        await self.artifacts.start()
        service = AsyncJobService(
            self.settings,
            self.store,
            self.artifacts,
            _successful_renderer,
        )
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="expires-during-fetch",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            claimed = await self.store.claim(
                now,
                self.settings.max_attempts,
            )
            stored = await self.artifacts.put(
                job.id,
                b"expires-after-fetch-starts",
                media_type="image/png",
                filename="expiring.png",
            )
            await self.store.succeed(
                claimed.id,
                expected_attempt=claimed.attempt_count,
                artifact_key=stored.key,
                media_type="image/png",
                filename="expiring.png",
                artifact_bytes=25,
                result_expires_at=stored.expires_at,
                queue_ms=0,
            )
            successful = await self.store.get(job.id, now)

            with patch("async_jobs.datetime", wraps=datetime) as clock:
                clock.now.side_effect = (
                    now,
                    stored.expires_at + timedelta(seconds=1),
                )
                self.assertIsNone(await service.result(successful))

            expired = await self.store.get(job.id, now)
            self.assertEqual(expired.status, "expired")
            self.assertEqual(expired.error_code, "async_result_expired")
            self.assertIsNone(expired.artifact_key)
            data_path, metadata_path = self.artifacts._paths(stored.key)
            self.assertFalse(data_path.exists())
            self.assertFalse(metadata_path.exists())
        finally:
            await self.artifacts.close()
            await self.store.close()

    async def test_result_expired_before_fetch_updates_and_deletes(self):
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=1)
        artifact_key = f"{uuid4()}.png"
        job = JobRecord(
            id=str(uuid4()),
            request_id="expired-before-fetch",
            status="succeeded",
            payload=None,
            attempt_count=1,
            available_at=now,
            queue_expires_at=deadline,
            created_at=now,
            completed_at=now,
            artifact_key=artifact_key,
            media_type="image/png",
            filename="expired.png",
            artifact_bytes=3,
            result_expires_at=deadline,
        )
        expired = JobRecord(
            **{
                **job.__dict__,
                "status": "expired",
                "error_code": "async_result_expired",
            }
        )
        job_store = SimpleNamespace(
            expire_result=AsyncMock(return_value=expired),
            acknowledge_artifact_deletion=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            get=AsyncMock(),
            delete=AsyncMock(),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            artifact_store,
            _successful_renderer,
        )

        with patch("async_jobs.datetime", wraps=datetime) as clock:
            clock.now.return_value = deadline + timedelta(seconds=1)
            self.assertIsNone(await service.result(job))

        artifact_store.get.assert_not_awaited()
        job_store.expire_result.assert_awaited_once_with(
            job.id,
            artifact_key,
            deadline + timedelta(seconds=1),
        )
        artifact_store.delete.assert_awaited_once_with(artifact_key)
        job_store.acknowledge_artifact_deletion.assert_awaited_once_with(
            artifact_key
        )

    async def test_cancel_normalizes_expired_queued_job(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="cancel-expired-queue",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now - timedelta(minutes=1),
                queue_expires_at=now - timedelta(seconds=1),
                created_at=now - timedelta(minutes=1),
            )
            await self.store.create(job, active_limit=1)

            cancelled = await self.store.cancel(job.id, now)

            self.assertEqual(cancelled.status, "expired")
            self.assertEqual(cancelled.error_code, "job_queue_expired")
            self.assertIsNone(cancelled.payload)
        finally:
            await self.store.close()

    async def test_cancel_normalizes_expired_successful_result(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="cancel-expired-result",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            await self.store.claim(now, self.settings.max_attempts)
            await self.store.succeed(
                job.id,
                expected_attempt=1,
                artifact_key="cancel-expired-result",
                media_type="image/png",
                filename="capture.png",
                artifact_bytes=3,
                result_expires_at=now + timedelta(seconds=1),
                queue_ms=0,
            )

            cancelled = await self.store.cancel(
                job.id,
                now + timedelta(seconds=2),
            )

            self.assertEqual(cancelled.status, "expired")
            self.assertEqual(
                cancelled.error_code,
                "async_result_expired",
            )
            self.assertEqual(
                cancelled.artifact_key,
                "cancel-expired-result",
            )
        finally:
            await self.store.close()

    async def test_create_expires_overdue_jobs_before_active_limit(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            expired = JobRecord(
                id=str(uuid4()),
                request_id="expired-active-slot",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now - timedelta(minutes=1),
                queue_expires_at=now - timedelta(seconds=1),
                created_at=now - timedelta(minutes=1),
            )
            await self.store.create(expired, active_limit=1)
            replacement = JobRecord(
                id=str(uuid4()),
                request_id="replacement-active-slot",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )

            created = await self.store.create(replacement, active_limit=1)
            previous = await self.store.get(expired.id, now)

            self.assertEqual(created.id, replacement.id)
            self.assertEqual(previous.status, "expired")
            self.assertIsNone(previous.payload)
        finally:
            await self.store.close()

    async def test_idempotent_create_normalizes_expired_result(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="expired-idempotent-result",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
                request_fingerprint=b"same-fingerprint",
            )
            await self.store.create(job, active_limit=1)
            await self.store.claim(now, self.settings.max_attempts)
            await self.store.succeed(
                job.id,
                expected_attempt=1,
                artifact_key="expired-idempotent-result",
                media_type="image/png",
                filename="capture.png",
                artifact_bytes=3,
                result_expires_at=now + timedelta(seconds=1),
                queue_ms=0,
            )
            repeated = JobRecord(
                **{
                    **job.__dict__,
                    "id": str(uuid4()),
                    "created_at": now + timedelta(seconds=2),
                }
            )

            existing = await self.store.create(repeated, active_limit=1)

            self.assertEqual(existing.id, job.id)
            self.assertEqual(existing.status, "expired")
            self.assertEqual(existing.error_code, "async_result_expired")
        finally:
            await self.store.close()

    async def test_claim_fails_queued_retry_at_a_lowered_attempt_cap(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="lowered-attempt-cap",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            first = await self.store.claim(now, max_attempts=3)
            await self.store.requeue(job.id, first.attempt_count, now)

            claimed = await self.store.claim(now, max_attempts=1)

            self.assertIsNone(claimed)
            current = await self.store.get(job.id, now)
            self.assertEqual(current.status, "failed")
            self.assertEqual(current.attempt_count, 1)
            self.assertEqual(current.error_code, "job_attempts_exhausted")
            self.assertIsNone(current.payload)
        finally:
            await self.store.close()

    async def test_claim_scrubs_only_when_attempts_clear_payload(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            with patch.object(
                SQLiteJobStore,
                "_scrub_payload_history",
                wraps=SQLiteJobStore._scrub_payload_history,
            ) as scrub:
                self.assertIsNone(await self.store.claim(now, max_attempts=3))
                scrub.assert_not_called()

            job = JobRecord(
                id=str(uuid4()),
                request_id="conditional-claim-scrub",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            with patch.object(
                SQLiteJobStore,
                "_scrub_payload_history",
                wraps=SQLiteJobStore._scrub_payload_history,
            ) as scrub:
                claimed = await self.store.claim(now, max_attempts=3)
                self.assertEqual(claimed.status, "running")
                scrub.assert_not_called()

            await self.store.requeue(job.id, claimed.attempt_count, now)
            with patch.object(
                SQLiteJobStore,
                "_scrub_payload_history",
                wraps=SQLiteJobStore._scrub_payload_history,
            ) as scrub:
                self.assertIsNone(await self.store.claim(now, max_attempts=1))
                scrub.assert_called_once()

            failed = await self.store.get(job.id, now)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.error_code, "job_attempts_exhausted")
            self.assertIsNone(failed.payload)
        finally:
            await self.store.close()

    async def test_maintenance_scrubs_only_when_queue_expiry_clears_payload(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            with patch.object(
                SQLiteJobStore,
                "_scrub_payload_history",
                wraps=SQLiteJobStore._scrub_payload_history,
            ) as scrub:
                self.assertEqual(await self.store.maintain(now), [])
                scrub.assert_not_called()

            job = JobRecord(
                id=str(uuid4()),
                request_id="conditional-maintenance-scrub",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(seconds=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            with patch.object(
                SQLiteJobStore,
                "_scrub_payload_history",
                wraps=SQLiteJobStore._scrub_payload_history,
            ) as scrub:
                self.assertEqual(await self.store.maintain(now), [])
                scrub.assert_not_called()

                self.assertEqual(
                    await self.store.maintain(now + timedelta(seconds=2)),
                    [],
                )
                scrub.assert_called_once()

                self.assertEqual(
                    await self.store.maintain(now + timedelta(seconds=3)),
                    [],
                )
                scrub.assert_called_once()

            expired = await self.store.get(
                job.id,
                now + timedelta(seconds=3),
            )
            self.assertEqual(expired.status, "expired")
            self.assertEqual(expired.error_code, "job_queue_expired")
            self.assertIsNone(expired.payload)
        finally:
            await self.store.close()

    async def test_queue_expiry_preserves_webhook_outbox(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="expired-webhook",
                status="queued",
                payload=b"encrypted",
                webhook_payload=b"encrypted-webhook",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(seconds=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            expired_at = now + timedelta(seconds=2)
            await self.store.maintain(expired_at)
            expired = await self.store.get_by_request_id(job.request_id)
            pending = await self.store.pending_notifications(expired_at)
            self.assertEqual(expired.status, "expired")
            self.assertEqual(expired.webhook_event_status, "failed")
            self.assertEqual(expired.webhook_payload, b"encrypted-webhook")
            self.assertEqual([item.id for item in pending], [job.id])
        finally:
            await self.store.close()

    async def test_cancellation_preserves_webhook_outbox(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="cancelled-webhook",
                status="queued",
                payload=b"encrypted",
                webhook_payload=b"encrypted-webhook",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            cancelled = await self.store.cancel(job.id, now)
            pending = await self.store.pending_notifications(now)
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(cancelled.webhook_event_status, "cancelled")
            self.assertEqual(cancelled.webhook_payload, b"encrypted-webhook")
            self.assertEqual([item.id for item in pending], [job.id])
        finally:
            await self.store.close()

    async def test_acknowledging_webhook_scrubs_payload_history(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="acknowledged-webhook",
                status="queued",
                payload=b"encrypted",
                webhook_payload=b"encrypted-webhook",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            await self.store.cancel(job.id, now)
            with patch.object(
                SQLiteJobStore,
                "_scrub_payload_history",
                wraps=SQLiteJobStore._scrub_payload_history,
            ) as scrub:
                await self.store.acknowledge_notification(
                    job.id, b"encrypted-webhook"
                )
                scrub.assert_called_once()

            acknowledged = await self.store.get_by_request_id(job.request_id)
            self.assertIsNone(acknowledged.webhook_payload)
        finally:
            await self.store.close()

    async def test_create_scrubs_only_when_expiring_queued_payload(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            expiring = JobRecord(
                id=str(uuid4()),
                request_id="create-scrub-expiring",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(seconds=1),
                created_at=now,
            )
            replacement = JobRecord(
                id=str(uuid4()),
                request_id="create-scrub-replacement",
                status="queued",
                payload=b"replacement-encrypted",
                attempt_count=0,
                available_at=now + timedelta(seconds=2),
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now + timedelta(seconds=2),
            )
            with patch.object(
                SQLiteJobStore,
                "_scrub_payload_history",
                wraps=SQLiteJobStore._scrub_payload_history,
            ) as scrub:
                await self.store.create(expiring, active_limit=1)
                scrub.assert_not_called()

                await self.store.create(replacement, active_limit=1)
                scrub.assert_called_once()

            expired = await self.store.get(
                expiring.id,
                now + timedelta(seconds=2),
            )
            self.assertEqual(expired.status, "expired")
            self.assertIsNone(expired.payload)
        finally:
            await self.store.close()

    async def test_busy_scrub_is_retried_without_another_payload_update(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="busy-scrub-retry",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(seconds=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            with patch.object(
                SQLiteJobStore,
                "_scrub_payload_history",
                side_effect=(
                    RuntimeError("SQLite payload scrub checkpoint was busy"),
                    None,
                ),
            ) as scrub:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "checkpoint was busy",
                ):
                    await self.store.maintain(now + timedelta(seconds=2))
                self.assertTrue(self.store._scrub_pending)

                self.assertEqual(
                    await self.store.maintain(now + timedelta(seconds=3)),
                    [],
                )
                self.assertEqual(scrub.call_count, 2)
                self.assertFalse(self.store._scrub_pending)

            expired = await self.store.get(
                job.id,
                now + timedelta(seconds=3),
            )
            self.assertEqual(expired.status, "expired")
            self.assertIsNone(expired.payload)
        finally:
            await self.store.close()

    async def test_existing_database_scrubs_recovery_wal_on_start(self):
        await self.store.start()
        await self.store.close()
        reopened = SQLiteJobStore(
            JobStoreConfig(self.root, self.settings.metadata_ttl)
        )
        with patch.object(
            SQLiteJobStore,
            "_scrub_payload_history",
            wraps=SQLiteJobStore._scrub_payload_history,
        ) as scrub:
            await reopened.start()
            scrub.assert_called_once()
        await reopened.close()

    async def test_claim_token_recovers_a_lost_acknowledgement(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="lost-claim-ack",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            token = str(uuid4())
            first = await self.store.claim(now, 3, token)
            repeated = await self.store.claim(now, 3, token)
            self.assertEqual(repeated.id, first.id)
            self.assertEqual(repeated.attempt_count, 1)
        finally:
            await self.store.close()

    async def test_stale_attempt_cannot_settle_a_newer_running_attempt(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="stale-terminal-transition",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            first = await self.store.claim(now, max_attempts=3)
            await self.store.requeue(job.id, first.attempt_count, now)
            second = await self.store.claim(now, max_attempts=3)

            with self.assertRaises(JobConflictError):
                await self.store.succeed(
                    job.id,
                    expected_attempt=first.attempt_count,
                    artifact_key="stale-result",
                    media_type="image/png",
                    filename="capture.png",
                    artifact_bytes=3,
                    result_expires_at=now + timedelta(minutes=1),
                    queue_ms=0,
                )
            with self.assertRaises(JobConflictError):
                await self.store.fail(
                    job.id,
                    expected_attempt=first.attempt_count,
                    code="stale_failure",
                    message="A stale attempt failed.",
                    retryable=False,
                )

            current = await self.store.get(job.id, now)
            self.assertEqual(current.status, "running")
            self.assertEqual(current.attempt_count, second.attempt_count)
        finally:
            await self.store.close()

    async def test_polling_shares_one_throttled_maintenance_run(self):
        job_store = SimpleNamespace(
            maintain=AsyncMock(return_value=[]),
            acknowledge_artifact_deletion=AsyncMock(),
            get=AsyncMock(return_value=None),
        )
        artifact_store = SimpleNamespace(
            maintain=AsyncMock(),
            delete=AsyncMock(),
        )
        service = AsyncJobService(
            _settings(self.root, poll_seconds=10),
            job_store,
            artifact_store,
            _successful_renderer,
        )

        await asyncio.gather(*(service.get("missing") for _ in range(20)))

        self.assertEqual(job_store.get.await_count, 20)
        job_store.maintain.assert_awaited_once()
        artifact_store.maintain.assert_awaited_once()

    async def test_maintenance_stops_after_shutdown_during_job_store_call(self):
        maintenance_started = asyncio.Event()

        async def cancellation_resistant_maintenance(_now):
            maintenance_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return ["must-not-delete"]

        job_store = SimpleNamespace(
            maintain=AsyncMock(side_effect=cancellation_resistant_maintenance),
            acknowledge_artifact_deletion=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            maintain=AsyncMock(),
            delete=AsyncMock(),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            artifact_store,
            _successful_renderer,
        )

        maintenance = asyncio.create_task(service._maintain(force=True))
        await asyncio.wait_for(maintenance_started.wait(), timeout=1)
        service._closing = True
        maintenance.cancel()
        await asyncio.wait_for(maintenance, timeout=1)

        artifact_store.delete.assert_not_awaited()
        job_store.acknowledge_artifact_deletion.assert_not_awaited()
        artifact_store.maintain.assert_not_awaited()

    async def test_maintenance_stops_after_shutdown_during_artifact_delete(self):
        deletion_started = asyncio.Event()

        async def cancellation_resistant_delete(_key):
            deletion_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return None

        job_store = SimpleNamespace(
            maintain=AsyncMock(return_value=["shutdown-delete"]),
            acknowledge_artifact_deletion=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            maintain=AsyncMock(),
            delete=AsyncMock(side_effect=cancellation_resistant_delete),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            artifact_store,
            _successful_renderer,
        )

        maintenance = asyncio.create_task(service._maintain(force=True))
        await asyncio.wait_for(deletion_started.wait(), timeout=1)
        service._closing = True
        maintenance.cancel()
        await asyncio.wait_for(maintenance, timeout=1)

        job_store.acknowledge_artifact_deletion.assert_not_awaited()
        artifact_store.maintain.assert_not_awaited()

    async def test_job_maintenance_failure_does_not_block_status_read(self):
        expected = SimpleNamespace(id="healthy-point-read")
        job_store = SimpleNamespace(
            maintain=AsyncMock(side_effect=RuntimeError("cleanup outage")),
            acknowledge_artifact_deletion=AsyncMock(),
            get=AsyncMock(return_value=expected),
        )
        artifact_store = SimpleNamespace(
            maintain=AsyncMock(),
            delete=AsyncMock(),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            artifact_store,
            _successful_renderer,
        )

        result = await service.get("healthy-point-read")

        self.assertIs(result, expected)
        job_store.maintain.assert_awaited_once()
        job_store.get.assert_awaited_once()

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
        claimed = await self.store.claim(datetime.now(UTC), settings.max_attempts)
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

    async def test_shutdown_leaves_playwright_style_close_error_running(self):
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
            self.assertEqual(current.status, "running")
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
        artifacts = LocalArtifactStore(
            ArtifactStoreConfig(self.root, timedelta(seconds=-1))
        )
        await artifacts.start()
        stored = await artifacts.put(
            str(uuid4()),
            b"old",
            media_type="image/png",
            filename="old.png",
        )
        self.assertIsNone(await artifacts.get(stored.key))
        data_path, metadata_path = artifacts._paths(stored.key)
        self.assertFalse(data_path.exists())
        self.assertFalse(metadata_path.exists())
        await artifacts.close()

    async def test_local_artifact_is_synced_before_put_returns(self):
        await self.artifacts.start()
        with patch(
            "async_job_providers.os.fsync",
            wraps=os.fsync,
        ) as sync:
            await self.artifacts.put(
                str(uuid4()),
                b"durable",
                media_type="image/png",
                filename="durable.png",
            )
        self.assertGreaterEqual(sync.call_count, 4)
        await self.artifacts.close()

    async def test_local_artifact_store_accepts_every_render_output(self):
        await self.artifacts.start()
        media = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
            "image/avif": "avif",
            "image/gif": "gif",
            "application/pdf": "pdf",
            "text/html; charset=utf-8": "html",
            "text/markdown; charset=utf-8": "md",
            "application/json": "json",
            "application/zip": "zip",
            "video/webm": "webm",
            "video/mp4": "mp4",
        }
        try:
            for media_type, extension in media.items():
                stored = await self.artifacts.put(
                    str(uuid4()),
                    b"artifact",
                    media_type=media_type,
                    filename=f"capture.{extension}",
                )
                self.assertTrue(stored.key.endswith(f".{extension}"))
                artifact = await self.artifacts.get(stored.key)
                self.assertEqual(artifact.media_type, media_type)
                self.assertEqual(artifact.body, b"artifact")
        finally:
            await self.artifacts.close()

    async def test_local_artifact_deletion_syncs_directory(self):
        await self.artifacts.start()
        stored = await self.artifacts.put(
            str(uuid4()),
            b"durably-deleted",
            media_type="image/png",
            filename="deleted.png",
        )
        data_path, metadata_path = self.artifacts._paths(stored.key)
        with patch.object(
            self.artifacts,
            "_sync_directory",
            wraps=self.artifacts._sync_directory,
        ) as sync:
            await self.artifacts.delete(stored.key)
            sync.assert_called_once_with()
            self.assertFalse(data_path.exists())
            self.assertFalse(metadata_path.exists())

            sync.reset_mock()
            await self.artifacts.delete(stored.key)
            sync.assert_not_called()
        await self.artifacts.close()

    async def test_local_artifact_reads_reject_symlinked_files(self):
        await self.artifacts.start()
        secret = self.root / "service-secret"
        secret.write_bytes(b"must-not-be-returned")
        secret.chmod(0o600)

        data_link = await self.artifacts.put(
            str(uuid4()),
            b"original-data",
            media_type="image/png",
            filename="data-link.png",
        )
        data_path, _metadata_path = self.artifacts._paths(data_link.key)
        data_path.unlink()
        data_path.symlink_to(secret)
        self.assertIsNone(await self.artifacts.get(data_link.key))

        metadata_link = await self.artifacts.put(
            str(uuid4()),
            b"original-metadata",
            media_type="image/png",
            filename="metadata-link.png",
        )
        _data_path, metadata_path = self.artifacts._paths(metadata_link.key)
        metadata_path.unlink()
        metadata_path.symlink_to(secret)
        self.assertIsNone(await self.artifacts.get(metadata_link.key))

        await self.artifacts.delete(data_link.key)
        await self.artifacts.delete(metadata_link.key)
        await self.artifacts.close()

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"),
        "FIFO open flags are POSIX-only",
    )
    async def test_local_artifact_fifo_candidates_open_nonblocking(self):
        await self.artifacts.start()
        original_open = os.open
        candidates: set[str] = set()
        opened: set[str] = set()

        def checked_open(path, flags, *args, **kwargs):
            candidate = os.fspath(path)
            if candidate in candidates:
                self.assertTrue(flags & os.O_NONBLOCK)
                opened.add(candidate)
            return original_open(path, flags, *args, **kwargs)

        data_fifo = await self.artifacts.put(
            str(uuid4()),
            b"replace-data-with-fifo",
            media_type="image/png",
            filename="data-fifo.png",
        )
        data_path, _metadata_path = self.artifacts._paths(data_fifo.key)
        data_path.unlink()
        os.mkfifo(data_path, 0o600)
        candidates.add(os.fspath(data_path))

        metadata_fifo = await self.artifacts.put(
            str(uuid4()),
            b"replace-metadata-with-fifo",
            media_type="image/png",
            filename="metadata-fifo.png",
        )
        _data_path, metadata_path = self.artifacts._paths(metadata_fifo.key)
        metadata_path.unlink()
        os.mkfifo(metadata_path, 0o600)
        candidates.add(os.fspath(metadata_path))

        with patch(
            "async_job_providers.os.open",
            side_effect=checked_open,
        ):
            self.assertIsNone(await self.artifacts.get(data_fifo.key))
            await self.artifacts.maintain(datetime.now(UTC))

        self.assertEqual(opened, candidates)
        await self.artifacts.delete(data_fifo.key)
        await self.artifacts.delete(metadata_fifo.key)
        await self.artifacts.close()

    async def test_local_artifact_read_propagates_transient_open_failure(self):
        await self.artifacts.start()
        stored = await self.artifacts.put(
            str(uuid4()),
            b"retry-after-emfile",
            media_type="image/png",
            filename="retry.png",
        )

        with patch(
            "async_job_providers.os.open",
            side_effect=OSError(errno.EMFILE, "too many open files"),
        ):
            with self.assertRaises(OSError) as error:
                await self.artifacts.get(stored.key)

        self.assertEqual(error.exception.errno, errno.EMFILE)
        data_path, metadata_path = self.artifacts._paths(stored.key)
        self.assertTrue(data_path.exists())
        self.assertTrue(metadata_path.exists())
        await self.artifacts.close()

    async def test_cancelled_local_artifact_read_waits_for_thread(self):
        read_started = threading.Event()
        release_read = threading.Event()

        class BlockingReadStore(LocalArtifactStore):
            def _get(self, key):
                read_started.set()
                release_read.wait(timeout=2)
                return super()._get(key)

        artifacts = BlockingReadStore(
            ArtifactStoreConfig(self.root, self.settings.result_ttl)
        )
        await artifacts.start()
        stored = await artifacts.put(
            str(uuid4()),
            b"bounded-read",
            media_type="image/png",
            filename="bounded.png",
        )
        reading = asyncio.create_task(artifacts.get(stored.key))
        try:
            started = await asyncio.to_thread(read_started.wait, 1)
            self.assertTrue(started)

            reading.cancel()
            await asyncio.sleep(0)
            self.assertFalse(reading.done())

            release_read.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(reading, timeout=1)
        finally:
            release_read.set()
            if not reading.done():
                reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
            await artifacts.close()

    async def test_artifact_directory_entry_is_synced_on_start(self):
        artifacts = LocalArtifactStore(
            ArtifactStoreConfig(
                self.root / "new-data-dir",
                self.settings.result_ttl,
            )
        )
        artifacts.config.data_dir.mkdir()
        with patch(
            "async_job_providers.os.fsync",
            wraps=os.fsync,
        ) as sync:
            await artifacts.start()

        self.assertGreaterEqual(sync.call_count, 1)
        await artifacts.close()

    async def test_artifact_maintenance_removes_abandoned_temp_files(self):
        await self.artifacts.start()
        temporary = self.artifacts.root / ".abandoned.data.tmp"
        temporary.write_bytes(b"partial")
        old = datetime.now(UTC) - timedelta(hours=2)
        os.utime(temporary, (old.timestamp(), old.timestamp()))

        await self.artifacts.maintain(datetime.now(UTC))

        self.assertFalse(temporary.exists())
        await self.artifacts.close()

    async def test_artifact_maintenance_durably_removes_orphan(self):
        await self.artifacts.start()
        key = f"{uuid4()}.png"
        orphan = self.artifacts.root / key
        orphan.write_bytes(b"uncommitted-upload")
        old = datetime.now(UTC) - self.settings.result_ttl - timedelta(seconds=1)
        os.utime(orphan, (old.timestamp(), old.timestamp()))

        with patch.object(
            self.artifacts,
            "_sync_directory",
            wraps=self.artifacts._sync_directory,
        ) as sync:
            await self.artifacts.maintain(datetime.now(UTC))

        self.assertFalse(orphan.exists())
        sync.assert_called_once_with()
        await self.artifacts.close()

    async def test_artifact_maintenance_removes_malformed_metadata(self):
        await self.artifacts.start()
        stored = await self.artifacts.put(
            str(uuid4()),
            b"corrupt-metadata-result",
            media_type="image/png",
            filename="corrupt.png",
        )
        data_path, metadata_path = self.artifacts._paths(stored.key)
        metadata_path.write_bytes(b"{not-json")
        metadata_path.chmod(0o600)

        with patch.object(
            self.artifacts,
            "_sync_directory",
            wraps=self.artifacts._sync_directory,
        ) as sync:
            await self.artifacts.maintain(datetime.now(UTC))

        self.assertFalse(data_path.exists())
        self.assertFalse(metadata_path.exists())
        sync.assert_called_once_with()
        await self.artifacts.close()

    async def test_artifact_maintenance_preserves_transient_open_failure(self):
        await self.artifacts.start()
        stored = await self.artifacts.put(
            str(uuid4()),
            b"preserve-on-emfile",
            media_type="image/png",
            filename="preserved.png",
        )
        data_path, metadata_path = self.artifacts._paths(stored.key)

        with patch(
            "async_job_providers.os.open",
            side_effect=OSError(errno.EMFILE, "too many open files"),
        ):
            await self.artifacts.maintain(datetime.now(UTC))

        self.assertTrue(data_path.exists())
        self.assertTrue(metadata_path.exists())
        await self.artifacts.close()

    async def test_artifact_maintenance_preserves_active_publication(self):
        publication_started = threading.Event()
        release_publication = threading.Event()

        class BlockingPublicationStore(LocalArtifactStore):
            sync_count = 0

            def _sync_directory(self):
                self.sync_count += 1
                if self.sync_count == 1:
                    publication_started.set()
                    release_publication.wait(timeout=2)
                return super()._sync_directory()

        artifacts = BlockingPublicationStore(
            ArtifactStoreConfig(self.root, timedelta(seconds=-1))
        )
        await artifacts.start()
        publication = asyncio.create_task(
            artifacts.put(
                str(uuid4()),
                b"publishing",
                media_type="image/png",
                filename="publishing.png",
            )
        )
        try:
            started = await asyncio.to_thread(
                publication_started.wait,
                1,
            )
            self.assertTrue(started)

            await artifacts.maintain(datetime.now(UTC))
            release_publication.set()
            stored = await publication
            data_path, metadata_path = artifacts._paths(stored.key)

            self.assertTrue(data_path.exists())
            self.assertTrue(metadata_path.exists())
        finally:
            release_publication.set()
            if not publication.done():
                await publication
            await artifacts.close()

    def test_artifact_directory_sync_is_skipped_on_windows(self):
        with (
            patch("async_job_providers.os.name", "nt"),
            patch("async_job_providers.os.open") as open_file,
        ):
            self.artifacts._sync_directory()
        open_file.assert_not_called()

    async def test_bundled_stores_refuse_unprotected_windows_data(self):
        with patch("async_job_providers.os.name", "nt"):
            with self.assertRaisesRegex(RuntimeError, "external job store"):
                await self.store.start()
            with self.assertRaisesRegex(RuntimeError, "external artifact store"):
                await self.artifacts.start()

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
            await store.claim(completed_at, settings.max_attempts)
            await store.succeed(
                job.id,
                expected_attempt=1,
                artifact_key="still-live",
                media_type="image/png",
                filename="capture.png",
                artifact_bytes=3,
                result_expires_at=now + timedelta(minutes=5),
                queue_ms=0,
            )

            expired_keys = await store.maintain(now)
            current = await store.get(job.id, now)
            self.assertNotIn("still-live", expired_keys)
            self.assertEqual(current.status, "succeeded")
            self.assertEqual(current.artifact_key, "still-live")
        finally:
            await store.close()

    async def test_metadata_cleanup_preserves_pending_webhook(self):
        settings = _settings(self.root, metadata_ttl=timedelta(seconds=1))
        store = SQLiteJobStore(
            JobStoreConfig(self.root, settings.metadata_ttl)
        )
        await store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="pending-webhook",
                status="queued",
                payload=b"encrypted",
                webhook_payload=b"encrypted-webhook",
                attempt_count=0,
                available_at=now - timedelta(seconds=3),
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now - timedelta(seconds=3),
                request_fingerprint=b"pending-webhook-fingerprint",
            )
            await store.create(job, active_limit=1)
            await store.claim(
                now - timedelta(seconds=3),
                settings.max_attempts,
                "pending-webhook-claim",
            )
            await store.fail(
                job.id,
                expected_attempt=1,
                code="render_failed",
                message="failed",
                retryable=False,
            )

            await store.maintain(now)
            current = await store.get_by_request_id(job.request_id)
            self.assertIsNotNone(current)
            self.assertEqual(current.webhook_event_status, "failed")
            self.assertEqual(current.webhook_payload, b"encrypted-webhook")
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

    async def test_shutdown_bounds_settlement_and_preserves_artifact(self):
        settlement_started = asyncio.Event()

        async def settle_success(*_args, **_kwargs):
            settlement_started.set()
            await asyncio.Event().wait()

        job_store = SimpleNamespace(
            succeed=AsyncMock(side_effect=settle_success),
            requeue=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            put=AsyncMock(
                return_value=StoredArtifact(
                    "committed-result",
                    datetime.now(UTC) + self.settings.result_ttl,
                )
            ),
            delete=AsyncMock(),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            artifact_store,
            _successful_renderer,
        )
        now = datetime.now(UTC)
        job_id = str(uuid4())
        job = JobRecord(
            id=job_id,
            request_id="cancel-during-settlement",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        processing = asyncio.create_task(service._process(job))
        await asyncio.wait_for(settlement_started.wait(), timeout=1)
        processing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(processing, timeout=0.25)

        artifact_store.delete.assert_not_awaited()
        job_store.requeue.assert_not_awaited()

    async def test_ambiguous_success_write_retries_without_deleting_artifact(self):
        succeed_calls = 0

        async def succeed(*_args, **_kwargs):
            nonlocal succeed_calls
            succeed_calls += 1
            if succeed_calls == 1:
                raise RuntimeError("commit acknowledgement was lost")

        job_store = SimpleNamespace(
            succeed=AsyncMock(side_effect=succeed),
            requeue=AsyncMock(),
            fail=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            put=AsyncMock(
                return_value=StoredArtifact(
                    "committed-result",
                    datetime.now(UTC) + self.settings.result_ttl,
                )
            ),
            delete=AsyncMock(),
        )
        settings = _settings(self.root, poll_seconds=0.001)
        service = AsyncJobService(
            settings,
            job_store,
            artifact_store,
            _successful_renderer,
        )
        now = datetime.now(UTC)
        job_id = str(uuid4())
        job = JobRecord(
            id=job_id,
            request_id="ambiguous-success",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        await asyncio.wait_for(service._process(job), timeout=1)
        self.assertEqual(succeed_calls, 2)
        artifact_store.delete.assert_not_awaited()
        job_store.requeue.assert_not_awaited()
        job_store.fail.assert_not_awaited()

    async def test_ambiguous_final_success_keeps_resolving_after_expiry(self):
        succeed_calls = 0

        async def succeed(*_args, **_kwargs):
            nonlocal succeed_calls
            succeed_calls += 1
            if succeed_calls <= 2:
                raise RuntimeError("commit acknowledgement was lost")

        job_store = SimpleNamespace(
            succeed=AsyncMock(side_effect=succeed),
            requeue=AsyncMock(),
            fail=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            put=AsyncMock(
                return_value=StoredArtifact(
                    "final-committed-result",
                    datetime.now(UTC) + timedelta(milliseconds=5),
                )
            ),
            delete=AsyncMock(),
        )
        settings = _settings(self.root, poll_seconds=0.01)
        service = AsyncJobService(
            settings,
            job_store,
            artifact_store,
            _successful_renderer,
        )
        now = datetime.now(UTC)
        job_id = str(uuid4())
        job = JobRecord(
            id=job_id,
            request_id="ambiguous-final-success",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        await asyncio.wait_for(service._process(job), timeout=1)

        self.assertEqual(succeed_calls, 3)
        artifact_store.delete.assert_not_awaited()
        job_store.requeue.assert_not_awaited()
        job_store.fail.assert_not_awaited()

    async def test_success_conflict_reconciles_winning_terminal_state(self):
        now = datetime.now(UTC)
        job_id = str(uuid4())
        winning = JobRecord(
            id=job_id,
            request_id="winning-terminal-state",
            status="failed",
            payload=None,
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
            completed_at=now,
            error_code="winning_failure",
            error_message="Another terminal transition won.",
            error_retryable=False,
        )
        job_store = SimpleNamespace(
            succeed=AsyncMock(side_effect=JobConflictError),
            get=AsyncMock(return_value=winning),
            requeue=AsyncMock(),
            fail=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            put=AsyncMock(
                return_value=StoredArtifact(
                    "unreferenced-conflicting-result",
                    now + self.settings.result_ttl,
                )
            ),
            delete=AsyncMock(),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            artifact_store,
            _successful_renderer,
        )
        job = JobRecord(
            id=job_id,
            request_id="winning-terminal-state",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        await asyncio.wait_for(service._process(job), timeout=1)

        job_store.get.assert_awaited_once()
        artifact_store.delete.assert_awaited_once_with(
            "unreferenced-conflicting-result"
        )
        job_store.requeue.assert_not_awaited()
        job_store.fail.assert_not_awaited()

    async def test_success_conflict_reconciles_lost_attempt_ownership(self):
        now = datetime.now(UTC)
        job_id = str(uuid4())
        newer_attempt = JobRecord(
            id=job_id,
            request_id="newer-running-attempt",
            status="running",
            payload=b"newer-encrypted-payload",
            attempt_count=2,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )
        job_store = SimpleNamespace(
            succeed=AsyncMock(side_effect=JobConflictError),
            get=AsyncMock(return_value=newer_attempt),
            requeue=AsyncMock(),
            fail=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            put=AsyncMock(
                return_value=StoredArtifact(
                    "lost-ownership-result",
                    now + self.settings.result_ttl,
                )
            ),
            delete=AsyncMock(),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            artifact_store,
            _successful_renderer,
        )
        job = JobRecord(
            id=job_id,
            request_id="original-running-attempt",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        await asyncio.wait_for(service._process(job), timeout=1)

        job_store.get.assert_awaited_once()
        artifact_store.delete.assert_awaited_once_with(
            "lost-ownership-result"
        )
        job_store.requeue.assert_not_awaited()
        job_store.fail.assert_not_awaited()

    async def test_result_ttl_starts_after_artifact_upload(self):
        upload_completed_at = None

        async def slow_put(*_args, **_kwargs):
            nonlocal upload_completed_at
            await asyncio.sleep(0.02)
            upload_completed_at = datetime.now(UTC)
            return StoredArtifact(
                "slow-upload",
                upload_completed_at + self.settings.result_ttl,
            )

        job_store = SimpleNamespace(
            succeed=AsyncMock(),
            requeue=AsyncMock(),
            fail=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            put=AsyncMock(side_effect=slow_put),
            delete=AsyncMock(),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            artifact_store,
            _successful_renderer,
        )
        now = datetime.now(UTC)
        job_id = str(uuid4())
        job = JobRecord(
            id=job_id,
            request_id="slow-upload",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        await service._process(job)

        expires_at = job_store.succeed.await_args.kwargs["result_expires_at"]
        self.assertEqual(
            expires_at,
            upload_completed_at + self.settings.result_ttl,
        )
        self.assertNotIn("expires_at", artifact_store.put.await_args.kwargs)

    async def test_shutdown_stops_after_cancellation_resistant_upload(self):
        upload_started = asyncio.Event()

        async def cancellation_resistant_put(*_args, **_kwargs):
            upload_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return StoredArtifact(
                    "shutdown-upload",
                    datetime.now(UTC) + self.settings.result_ttl,
                )

        job_store = SimpleNamespace(
            succeed=AsyncMock(),
            requeue=AsyncMock(),
            fail=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            put=AsyncMock(side_effect=cancellation_resistant_put),
            delete=AsyncMock(),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            artifact_store,
            _successful_renderer,
        )
        now = datetime.now(UTC)
        job_id = str(uuid4())
        job = JobRecord(
            id=job_id,
            request_id="shutdown-upload",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        processing = asyncio.create_task(service._process(job))
        await asyncio.wait_for(upload_started.wait(), timeout=1)
        service._closing = True
        processing.cancel()
        await asyncio.wait_for(processing, timeout=1)

        job_store.succeed.assert_not_awaited()
        job_store.requeue.assert_not_awaited()
        job_store.fail.assert_not_awaited()
        artifact_store.delete.assert_not_awaited()

    async def test_shutdown_stops_after_cancellation_resistant_render(self):
        render_started = asyncio.Event()

        async def cancellation_resistant_renderer(_payload):
            render_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await _successful_renderer(_payload)

        job_store = SimpleNamespace(
            succeed=AsyncMock(),
            requeue=AsyncMock(),
            fail=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            put=AsyncMock(),
            delete=AsyncMock(),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            artifact_store,
            cancellation_resistant_renderer,
        )
        now = datetime.now(UTC)
        job_id = str(uuid4())
        job = JobRecord(
            id=job_id,
            request_id="shutdown-render",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        processing = asyncio.create_task(service._process(job))
        await asyncio.wait_for(render_started.wait(), timeout=1)
        service._closing = True
        processing.cancel()
        await asyncio.wait_for(processing, timeout=1)

        artifact_store.put.assert_not_awaited()
        artifact_store.delete.assert_not_awaited()
        job_store.succeed.assert_not_awaited()
        job_store.requeue.assert_not_awaited()
        job_store.fail.assert_not_awaited()

    async def test_local_result_ttl_starts_after_metadata_persistence(self):
        class SlowMetadataStore(LocalArtifactStore):
            @staticmethod
            def _write_durable(path, body):
                if path.name.endswith(".json.tmp"):
                    import time

                    time.sleep(0.03)
                LocalArtifactStore._write_durable(path, body)

        ttl = timedelta(milliseconds=100)
        artifacts = SlowMetadataStore(ArtifactStoreConfig(self.root, ttl))
        await artifacts.start()
        try:
            stored = await artifacts.put(
                str(uuid4()),
                b"slow-metadata",
                media_type="image/png",
                filename="capture.png",
            )
            remaining = stored.expires_at - datetime.now(UTC)
            self.assertGreater(remaining, timedelta(milliseconds=85))
        finally:
            await artifacts.close()

    async def test_success_timing_includes_settlement_retries(self):
        class DelayedSuccessStore(SQLiteJobStore):
            succeed_calls = 0
            first_failure_at = None

            async def succeed(self, *args, **kwargs):
                self.succeed_calls += 1
                if self.succeed_calls == 1:
                    await asyncio.sleep(0.02)
                    self.first_failure_at = datetime.now(UTC)
                    raise RuntimeError("temporary settlement outage")
                return await super().succeed(*args, **kwargs)

        settings = _settings(self.root, poll_seconds=0.001)
        store = DelayedSuccessStore(
            JobStoreConfig(self.root, settings.metadata_ttl)
        )
        artifacts = LocalArtifactStore(
            ArtifactStoreConfig(self.root, settings.result_ttl)
        )
        service = AsyncJobService(
            settings,
            store,
            artifacts,
            _successful_renderer,
        )
        await store.start()
        await artifacts.start()
        try:
            job = await service.submit(
                _payload(),
                request_id="settlement-timing",
            )
            claimed = await store.claim(
                datetime.now(UTC),
                settings.max_attempts,
            )

            await service._process(claimed)

            current = await store.get(job.id, datetime.now(UTC))
            self.assertEqual(store.succeed_calls, 2)
            self.assertGreaterEqual(current.completed_at, store.first_failure_at)
            self.assertGreaterEqual(current.render_ms, 20)
        finally:
            await artifacts.close()
            await store.close()

    async def test_shutdown_leaves_final_attempt_for_startup_recovery(self):
        rendering = asyncio.Event()

        async def blocked_renderer(_payload):
            rendering.set()
            await asyncio.Event().wait()

        job_store = SimpleNamespace(
            requeue=AsyncMock(),
            fail=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            delete=AsyncMock(),
        )
        settings = _settings(self.root, max_attempts=1)
        service = AsyncJobService(
            settings,
            job_store,
            artifact_store,
            blocked_renderer,
        )
        now = datetime.now(UTC)
        job_id = str(uuid4())
        job = JobRecord(
            id=job_id,
            request_id="final-attempt-shutdown",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=settings.max_attempts,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        processing = asyncio.create_task(service._process(job))
        await asyncio.wait_for(rendering.wait(), timeout=1)
        processing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(processing, timeout=0.25)
        job_store.requeue.assert_not_awaited()
        job_store.fail.assert_not_awaited()

    async def test_worker_does_not_lose_wakeup_between_claim_and_wait(self):
        class InjectingStore(SQLiteJobStore):
            injected_job = None
            service = None

            async def claim(self, now, max_attempts, claim_token):
                if self.injected_job is not None:
                    job, self.injected_job = self.injected_job, None
                    await self.create(job, active_limit=2)
                    self.service._wake_workers()
                    return None
                return await super().claim(now, max_attempts, claim_token)

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

    async def test_workers_have_independent_wake_notifications(self):
        service = AsyncJobService(
            _settings(self.root, worker_count=2),
            SimpleNamespace(),
            SimpleNamespace(),
            _successful_renderer,
        )

        service._wake_workers()
        service._wakeups[0].clear()

        self.assertFalse(service._wakeups[0].is_set())
        self.assertTrue(service._wakeups[1].is_set())

    async def test_worker_retries_requeue_state_transition_error(self):
        recovered = asyncio.Event()
        claims = 0
        requeues = 0
        now = datetime.now(UTC)
        job_id = str(uuid4())

        async def claim(_now, _max_attempts, _claim_token):
            nonlocal claims
            claims += 1
            return job if claims == 1 else None

        async def requeue(_job_id, _expected_attempt, _available_at):
            nonlocal requeues
            requeues += 1
            if requeues == 1:
                raise RuntimeError("temporary state-store failure")
            recovered.set()

        job_store = SimpleNamespace(
            maintain=AsyncMock(return_value=[]),
            claim=AsyncMock(side_effect=claim),
            requeue=AsyncMock(side_effect=requeue),
            fail=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            maintain=AsyncMock(),
            delete=AsyncMock(),
        )

        async def retryable_failure(_payload):
            raise RenderError(
                "render_failed",
                "The image render failed.",
                500,
                True,
            )

        settings = _settings(self.root, poll_seconds=0.001)
        service = AsyncJobService(
            settings,
            job_store,
            artifact_store,
            retryable_failure,
        )
        job = JobRecord(
            id=job_id,
            request_id="transition-recovery",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        worker = asyncio.create_task(service._worker(0))
        try:
            await asyncio.wait_for(recovered.wait(), timeout=1)
            self.assertEqual(requeues, 2)
            job_store.fail.assert_not_awaited()
        finally:
            service._closing = True
            service._wake_workers()
            await asyncio.wait_for(worker, timeout=1)

    async def test_nonretryable_failure_retries_fail_transition(self):
        failed = asyncio.Event()
        fail_calls = 0

        async def fail(*_args, **_kwargs):
            nonlocal fail_calls
            fail_calls += 1
            if fail_calls == 1:
                raise RuntimeError("temporary state-store failure")
            failed.set()

        job_store = SimpleNamespace(
            fail=AsyncMock(side_effect=fail),
            requeue=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            delete=AsyncMock(),
        )

        async def nonretryable_failure(_payload):
            raise RenderError(
                "invalid_target",
                "The target cannot be rendered.",
                400,
                False,
            )

        settings = _settings(self.root, poll_seconds=0.001)
        service = AsyncJobService(
            settings,
            job_store,
            artifact_store,
            nonretryable_failure,
        )
        now = datetime.now(UTC)
        job_id = str(uuid4())
        job = JobRecord(
            id=job_id,
            request_id="terminal-transition-recovery",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=settings.max_attempts,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        await asyncio.wait_for(service._process(job), timeout=1)
        self.assertTrue(failed.is_set())
        self.assertEqual(fail_calls, 2)
        job_store.requeue.assert_not_awaited()

    async def test_worker_does_not_render_claim_returned_during_shutdown(self):
        claim_started = asyncio.Event()
        claimed_job = JobRecord(
            id=str(uuid4()),
            request_id="shutdown-claim",
            status="running",
            payload=b"encrypted",
            attempt_count=1,
            available_at=datetime.now(UTC),
            queue_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            created_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
        )

        async def cancellation_resistant_claim(*_args):
            claim_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return claimed_job

        job_store = SimpleNamespace(
            claim=AsyncMock(side_effect=cancellation_resistant_claim),
            maintain=AsyncMock(return_value=[]),
            acknowledge_artifact_deletion=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            maintain=AsyncMock(),
            delete=AsyncMock(),
        )
        renderer = AsyncMock()
        service = AsyncJobService(
            self.settings,
            job_store,
            artifact_store,
            renderer,
        )

        worker = asyncio.create_task(service._worker(0))
        await asyncio.wait_for(claim_started.wait(), timeout=1)
        service._closing = True
        worker.cancel()
        await asyncio.wait_for(worker, timeout=1)

        renderer.assert_not_awaited()

    async def test_claim_retry_stops_when_shutdown_begins(self):
        claim_started = asyncio.Event()
        claim_calls = 0

        async def cancellation_resistant_claim(*_args):
            nonlocal claim_calls
            claim_calls += 1
            claim_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("remote claim failed during shutdown")

        service = AsyncJobService(
            self.settings,
            SimpleNamespace(
                claim=AsyncMock(side_effect=cancellation_resistant_claim),
                maintain=AsyncMock(return_value=[]),
                acknowledge_artifact_deletion=AsyncMock(),
            ),
            SimpleNamespace(
                maintain=AsyncMock(),
                delete=AsyncMock(),
            ),
            AsyncMock(),
        )

        worker = asyncio.create_task(service._worker(0))
        await asyncio.wait_for(claim_started.wait(), timeout=1)
        service._closing = True
        worker.cancel()
        await asyncio.wait_for(worker, timeout=1)

        self.assertEqual(claim_calls, 1)

    async def test_transition_retry_stops_when_shutdown_begins(self):
        transition_started = asyncio.Event()
        transition_calls = 0

        async def cancellation_resistant_transition():
            nonlocal transition_calls
            transition_calls += 1
            transition_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("remote transition failed during shutdown")

        service = AsyncJobService(
            self.settings,
            SimpleNamespace(),
            SimpleNamespace(),
            AsyncMock(),
        )
        transition = asyncio.create_task(
            service._retry_state_transition(
                "fail",
                cancellation_resistant_transition,
                AsyncMock(return_value=False),
            )
        )
        await asyncio.wait_for(transition_started.wait(), timeout=1)
        service._closing = True
        transition.cancel()
        await asyncio.wait_for(transition, timeout=1)

        self.assertEqual(transition_calls, 1)

    async def test_failure_conflict_reconciles_winning_terminal_state(self):
        now = datetime.now(UTC)
        job_id = str(uuid4())
        winning = JobRecord(
            id=job_id,
            request_id="winning-failure-transition",
            status="cancelled",
            payload=None,
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
            completed_at=now,
            error_code="job_cancelled",
            error_message="Another terminal transition won.",
            error_retryable=False,
        )

        async def rejected_renderer(_payload):
            raise RenderError(
                "invalid_target",
                "The target is invalid.",
                400,
                False,
            )

        job_store = SimpleNamespace(
            fail=AsyncMock(side_effect=JobConflictError),
            get=AsyncMock(return_value=winning),
            requeue=AsyncMock(),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            SimpleNamespace(delete=AsyncMock()),
            rejected_renderer,
        )
        job = JobRecord(
            id=job_id,
            request_id="winning-failure-transition",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        await asyncio.wait_for(service._process(job), timeout=1)

        job_store.fail.assert_awaited_once()
        job_store.get.assert_awaited_once()
        job_store.requeue.assert_not_awaited()

    async def test_failure_conflict_accepts_deleted_winner(self):
        now = datetime.now(UTC)
        job_id = str(uuid4())

        async def rejected_renderer(_payload):
            raise RenderError(
                "invalid_target",
                "The target is invalid.",
                400,
                False,
            )

        job_store = SimpleNamespace(
            fail=AsyncMock(side_effect=JobConflictError),
            get=AsyncMock(return_value=None),
            requeue=AsyncMock(),
        )
        service = AsyncJobService(
            self.settings,
            job_store,
            SimpleNamespace(delete=AsyncMock()),
            rejected_renderer,
        )
        job = JobRecord(
            id=job_id,
            request_id="deleted-conflict-winner",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        await asyncio.wait_for(service._process(job), timeout=1)

        job_store.fail.assert_awaited_once()
        job_store.get.assert_awaited_once()
        job_store.requeue.assert_not_awaited()

    async def test_shutdown_retries_nonretryable_failure_once(self):
        first_failure = asyncio.Event()
        settled = asyncio.Event()
        fail_calls = 0

        async def fail(*_args, **_kwargs):
            nonlocal fail_calls
            fail_calls += 1
            if fail_calls == 1:
                first_failure.set()
                raise RuntimeError("temporary state-store failure")
            settled.set()

        job_store = SimpleNamespace(
            fail=AsyncMock(side_effect=fail),
            requeue=AsyncMock(),
        )
        artifact_store = SimpleNamespace(delete=AsyncMock())

        async def nonretryable_failure(_payload):
            raise RenderError(
                "invalid_target",
                "The target cannot be rendered.",
                400,
                False,
            )

        settings = _settings(self.root, poll_seconds=1)
        service = AsyncJobService(
            settings,
            job_store,
            artifact_store,
            nonretryable_failure,
        )
        now = datetime.now(UTC)
        job_id = str(uuid4())
        job = JobRecord(
            id=job_id,
            request_id="shutdown-terminal-intent",
            status="running",
            payload=service.cipher.encrypt(job_id, _payload()),
            attempt_count=1,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
            started_at=now,
        )

        processing = asyncio.create_task(service._process(job))
        await asyncio.wait_for(first_failure.wait(), timeout=1)
        processing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(processing, timeout=0.25)

        self.assertTrue(settled.is_set())
        self.assertEqual(fail_calls, 2)
        job_store.requeue.assert_not_awaited()

    async def test_sqlite_failure_transition_is_idempotent(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="idempotent-failure",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            await self.store.claim(now, self.settings.max_attempts)
            arguments = {
                "expected_attempt": 1,
                "code": "invalid_target",
                "message": "The target cannot be rendered.",
                "retryable": False,
            }
            await self.store.fail(job.id, **arguments)
            await self.store.fail(job.id, **arguments)
            current = await self.store.get(job.id, now)
            self.assertEqual(current.status, "failed")
            self.assertEqual(current.error_code, "invalid_target")
        finally:
            await self.store.close()

    async def test_sqlite_database_and_sidecars_are_owner_only(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="private-sqlite-files",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
            await self.store.create(job, active_limit=1)
            paths = list(self.root.glob("async-jobs.sqlite3*"))
            self.assertGreaterEqual(len(paths), 2)
            for path in paths:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        finally:
            await self.store.close()

    async def test_symlinked_sqlite_database_is_rejected(self):
        data_dir = self.root / "symlinked-sqlite"
        data_dir.mkdir()
        target = self.root / "database-target"
        target.write_bytes(b"not a database")
        target.chmod(0o600)
        (data_dir / "async-jobs.sqlite3").symlink_to(target)
        store = SQLiteJobStore(
            JobStoreConfig(data_dir, self.settings.metadata_ttl)
        )

        with self.assertRaisesRegex(RuntimeError, "private regular file"):
            await store.start()

        self.assertEqual(target.read_bytes(), b"not a database")

    async def test_unsafe_sqlite_sidecars_are_rejected_before_recovery(self):
        symlink_data = self.root / "symlinked-wal"
        symlink_data.mkdir(mode=0o700)
        symlink_data.chmod(0o700)
        target = self.root / "wal-target"
        target.write_bytes(b"untrusted recovery state")
        target.chmod(0o600)
        Path(f"{symlink_data / 'async-jobs.sqlite3'}-wal").symlink_to(target)
        symlink_store = SQLiteJobStore(
            JobStoreConfig(symlink_data, self.settings.metadata_ttl)
        )

        with self.assertRaisesRegex(RuntimeError, "private regular file"):
            await symlink_store.start()

        self.assertEqual(target.read_bytes(), b"untrusted recovery state")

        permissive_data = self.root / "permissive-wal"
        permissive_data.mkdir(mode=0o700)
        permissive_data.chmod(0o700)
        wal = Path(f"{permissive_data / 'async-jobs.sqlite3'}-wal")
        wal.write_bytes(b"untrusted recovery state")
        wal.chmod(0o644)
        permissive_store = SQLiteJobStore(
            JobStoreConfig(permissive_data, self.settings.metadata_ttl)
        )

        with self.assertRaisesRegex(RuntimeError, "owner-only"):
            await permissive_store.start()

        journal_data = self.root / "permissive-journal"
        journal_data.mkdir(mode=0o700)
        journal_data.chmod(0o700)
        journal = Path(f"{journal_data / 'async-jobs.sqlite3'}-journal")
        journal.write_bytes(b"untrusted rollback state")
        journal.chmod(0o644)
        journal_store = SQLiteJobStore(
            JobStoreConfig(journal_data, self.settings.metadata_ttl)
        )

        with self.assertRaisesRegex(RuntimeError, "owner-only"):
            await journal_store.start()

    async def test_symlinked_local_state_directories_are_rejected(self):
        redirected_data = self.root / "redirected-data"
        redirected_data.mkdir()
        symlinked_data = self.root / "symlinked-data"
        symlinked_data.symlink_to(redirected_data, target_is_directory=True)
        store = SQLiteJobStore(
            JobStoreConfig(symlinked_data, self.settings.metadata_ttl)
        )

        with self.assertRaisesRegex(RuntimeError, "only directories"):
            await store.start()

        artifact_data = self.root / "artifact-data"
        artifact_data.mkdir(mode=0o700)
        artifact_data.chmod(0o700)
        redirected_results = self.root / "redirected-results"
        redirected_results.mkdir()
        (artifact_data / "job-results").symlink_to(
            redirected_results,
            target_is_directory=True,
        )
        artifacts = LocalArtifactStore(
            ArtifactStoreConfig(artifact_data, self.settings.result_ttl)
        )

        with self.assertRaisesRegex(RuntimeError, "only directories"):
            await artifacts.start()

        self.assertEqual(list(redirected_data.iterdir()), [])
        self.assertEqual(list(redirected_results.iterdir()), [])

    async def test_unsafe_local_state_ancestor_is_rejected(self):
        unsafe_parent = self.root / "unsafe-parent"
        unsafe_parent.mkdir(mode=0o777)
        unsafe_parent.chmod(0o777)
        data_dir = unsafe_parent / "private-data"
        store = SQLiteJobStore(
            JobStoreConfig(data_dir, self.settings.metadata_ttl)
        )

        with self.assertRaisesRegex(RuntimeError, "unsafe directory"):
            await store.start()

        self.assertFalse(data_dir.exists())

    async def test_terminal_transition_scrubs_payload_history(self):
        await self.store.start()
        marker = b"terminal-payload-must-not-survive"
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="payload-scrub",
                status="queued",
                payload=marker,
                attempt_count=0,
                available_at=now,
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now,
                request_fingerprint=b"payload-scrub-fingerprint",
            )
            await self.store.create(job, active_limit=1)
            await self.store.cancel(job.id, now)
        finally:
            await self.store.close()

        for path in self.root.glob("async-jobs.sqlite3*"):
            self.assertNotIn(marker, path.read_bytes(), str(path))

    async def test_sqlite_database_and_wal_entries_are_synced_on_start(self):
        from async_job_providers import _sync_directory

        data_dir = self.root / "new-sqlite-data"
        store = SQLiteJobStore(
            JobStoreConfig(data_dir, self.settings.metadata_ttl)
        )
        sync_states = []

        def track_sync(path):
            sync_states.append(
                (
                    path,
                    Path(f"{store.path}-wal").exists(),
                )
            )
            _sync_directory(path)

        with patch(
            "async_job_providers._sync_directory",
            side_effect=track_sync,
        ):
            await store.start()
        try:
            self.assertIn((data_dir, True), sync_states)
        finally:
            await store.close()

    async def test_expired_artifact_key_remains_until_deletion_acknowledged(self):
        await self.store.start()
        try:
            now = datetime.now(UTC)
            job = JobRecord(
                id=str(uuid4()),
                request_id="deletion-retry",
                status="queued",
                payload=b"encrypted",
                attempt_count=0,
                available_at=now - timedelta(seconds=2),
                queue_expires_at=now + timedelta(minutes=1),
                created_at=now - timedelta(seconds=2),
            )
            await self.store.create(job, active_limit=1)
            await self.store.claim(
                now - timedelta(seconds=1),
                self.settings.max_attempts,
            )
            succeeded = await self.store.succeed(
                job.id,
                expected_attempt=1,
                artifact_key="retry-delete",
                media_type="image/png",
                filename="capture.png",
                artifact_bytes=3,
                result_expires_at=now + timedelta(seconds=1),
                queue_ms=0,
            )
            repeated = await self.store.succeed(
                job.id,
                expected_attempt=1,
                artifact_key="retry-delete",
                media_type="image/png",
                filename="capture.png",
                artifact_bytes=3,
                result_expires_at=now + timedelta(seconds=1),
                queue_ms=0,
            )
            self.assertEqual(repeated, succeeded)

            maintenance_time = now + timedelta(seconds=2)
            first = await self.store.maintain(maintenance_time)
            current = await self.store.get(job.id, maintenance_time)
            self.assertIn("retry-delete", first)
            self.assertEqual(current.status, "expired")
            self.assertEqual(current.artifact_key, "retry-delete")

            second = await self.store.maintain(maintenance_time)
            self.assertIn("retry-delete", second)
            await self.store.acknowledge_artifact_deletion("retry-delete")
            third = await self.store.maintain(maintenance_time)
            current = await self.store.get(job.id, maintenance_time)
            self.assertNotIn("retry-delete", third)
            self.assertIsNone(current.artifact_key)
        finally:
            await self.store.close()

class ProviderLoadingTests(unittest.TestCase):
    def test_cache_defaults_to_data_directory_without_async_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ)
            environment.update(
                {
                    "VIPERCAPTURE_ASYNC_JOBS": "0",
                    "VIPERCAPTURE_DATA_DIR": directory,
                }
            )
            environment.pop("VIPERCAPTURE_CACHE_DIR", None)
            result = subprocess.run(
                [sys.executable, "-c", "import main; print(main.CACHE_DIRECTORY)"],
                cwd=Path(__file__).parent,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.stdout.strip(), str(Path(directory) / "cache"))
    def test_generated_local_key_is_private_and_restart_stable(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=False,
        ):
            os.environ.pop("VIPERCAPTURE_JOB_SECRET", None)
            root = Path(directory)
            with (
                patch("async_jobs.os.fsync", wraps=os.fsync) as sync,
                patch("async_jobs.os.link", wraps=os.link) as link,
            ):
                first = PayloadCipher.for_data_dir(root)
            self.assertGreaterEqual(sync.call_count, 2)
            link.assert_called_once()
            self.assertEqual(list(root.glob(".async-jobs.key.*.tmp")), [])
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

    def test_new_key_data_directory_entry_is_synced(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=False,
        ):
            os.environ.pop("VIPERCAPTURE_JOB_SECRET", None)
            base = Path(directory)
            root = base / "first" / "second" / "new-data-directory"
            with patch(
                "async_jobs._sync_directory",
                wraps=__import__("async_jobs")._sync_directory,
            ) as sync:
                PayloadCipher.for_data_dir(root)

            sync.assert_any_call(base)
            sync.assert_any_call(base / "first")
            sync.assert_any_call(base / "first" / "second")
            sync.assert_any_call(root)

    def test_insecure_existing_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=False,
        ):
            os.environ.pop("VIPERCAPTURE_JOB_SECRET", None)
            root = Path(directory)
            key = root / "async-jobs.key"
            key.write_bytes(b"x" * 32)
            key.chmod(0o644)

            with self.assertRaisesRegex(RuntimeError, "owner-only"):
                PayloadCipher.for_data_dir(root)

    def test_symlinked_existing_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=False,
        ):
            os.environ.pop("VIPERCAPTURE_JOB_SECRET", None)
            root = Path(directory)
            target = root / "attacker-key"
            target.write_bytes(b"x" * 32)
            target.chmod(0o600)
            (root / "async-jobs.key").symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "private regular file"):
                PayloadCipher.for_data_dir(root)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"),
        "FIFO open flags are POSIX-only",
    )
    def test_fifo_key_candidate_is_opened_nonblocking(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=False,
        ):
            os.environ.pop("VIPERCAPTURE_JOB_SECRET", None)
            root = Path(directory)
            key = root / "async-jobs.key"
            os.mkfifo(key, 0o600)
            original_open = os.open

            def checked_open(path, flags, *args, **kwargs):
                if os.fspath(path) == os.fspath(key):
                    self.assertTrue(flags & os.O_NONBLOCK)
                return original_open(path, flags, *args, **kwargs)

            with patch("async_jobs.os.open", side_effect=checked_open):
                with self.assertRaisesRegex(RuntimeError, "regular file"):
                    PayloadCipher.for_data_dir(root)

    def test_key_directory_sync_is_skipped_on_windows(self):
        directory = Path(".")
        with (
            patch("async_jobs.os.name", "nt"),
            patch("async_jobs.os.open") as open_file,
        ):
            from async_jobs import _sync_directory

            _sync_directory(directory)
        open_file.assert_not_called()

    def test_external_factories_receive_provider_specific_config(self):
        module = ModuleType("test_async_provider_module")
        job_store = AsyncMock()
        artifact_store = AsyncMock()
        for provider, methods in (
            (
                job_store,
                (
                    "start", "close", "create", "claim",
                    "get", "cancel",
                    "succeed", "fail", "requeue_running", "requeue", "defer", "maintain",
                    "expire_result",
                    "acknowledge_artifact_deletion",
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
        self.original_result_slots = getattr(
            main.app.state,
            "async_result_slots",
            None,
        )
        main.app.state.async_result_slots = asyncio.Semaphore(2)
        self.request = SimpleNamespace(
            is_disconnected=AsyncMock(return_value=False)
        )
        self.service = SimpleNamespace(
            submit=AsyncMock(),
            existing=AsyncMock(return_value=None),
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
        main.app.state.async_result_slots = self.original_result_slots

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
        result = await main.read_render_job_result(self.job.id, self.request)
        self.assertEqual(result.status_code, 200)
        body = b"".join([chunk async for chunk in result.body_iterator])
        self.assertEqual(body, b"png")

    async def test_idempotent_webhook_replay_skips_new_validation(self):
        original_webhooks = getattr(main.app.state, "webhooks", None)
        main.app.state.webhooks = None
        payload = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "delivery": {
                    "webhook_url": "https://callback.example/hook"
                },
            }
        )
        self.service.existing.return_value = self.job
        try:
            response = await main.create_render_job(
                payload,
                SimpleNamespace(
                    state=SimpleNamespace(request_id="route-job")
                ),
            )
        finally:
            main.app.state.webhooks = original_webhooks
        self.assertEqual(response.status_code, 202)
        self.service.submit.assert_not_awaited()

    async def test_result_downloads_hold_a_concurrency_slot_while_streaming(self):
        succeeded = self.job.__class__(
            **{
                **self.job.__dict__,
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
        main.app.state.async_result_slots = asyncio.Semaphore(1)

        first = await main.read_render_job_result(self.job.id, self.request)
        second_task = asyncio.create_task(
            main.read_render_job_result(self.job.id, self.request)
        )
        await asyncio.sleep(0)
        self.assertFalse(second_task.done())

        first_body = b"".join(
            [chunk async for chunk in first.body_iterator]
        )
        second = await asyncio.wait_for(second_task, timeout=1)
        second_body = b"".join(
            [chunk async for chunk in second.body_iterator]
        )
        self.assertEqual(first_body, b"png")
        self.assertEqual(second_body, b"png")

    async def test_disconnected_result_waiter_does_not_load_artifact(self):
        succeeded = self.job.__class__(
            **{
                **self.job.__dict__,
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
        main.app.state.async_result_slots = asyncio.Semaphore(0)
        request = SimpleNamespace(
            is_disconnected=AsyncMock(return_value=True)
        )

        with self.assertRaises(RenderError) as error:
            await main.read_render_job_result(self.job.id, request)

        self.assertEqual(error.exception.code, "client_disconnected")
        self.service.result.assert_not_awaited()

    async def test_result_slot_is_released_when_disconnect_loses_race(self):
        succeeded = self.job.__class__(
            **{
                **self.job.__dict__,
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
        slots = asyncio.Semaphore(0)
        main.app.state.async_result_slots = slots
        disconnect_checks = 0

        async def is_disconnected():
            nonlocal disconnect_checks
            disconnect_checks += 1
            if disconnect_checks == 1:
                slots.release()
                await asyncio.sleep(0)
            return True

        request = SimpleNamespace(is_disconnected=is_disconnected)
        with self.assertRaises(RenderError) as error:
            await asyncio.wait_for(
                main.read_render_job_result(self.job.id, request),
                timeout=1,
            )

        self.assertEqual(error.exception.code, "client_disconnected")
        await asyncio.wait_for(slots.acquire(), timeout=0.1)
        slots.release()
        self.service.result.assert_not_awaited()

    async def test_result_slot_is_released_on_request_cancellation(self):
        succeeded = self.job.__class__(
            **{
                **self.job.__dict__,
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
        slots = asyncio.Semaphore(0)
        main.app.state.async_result_slots = slots
        disconnect_probe = asyncio.Event()

        async def is_disconnected():
            slots.release()
            await asyncio.sleep(0)
            disconnect_probe.set()
            await asyncio.Event().wait()

        request = SimpleNamespace(is_disconnected=is_disconnected)
        reading = asyncio.create_task(
            main.read_render_job_result(self.job.id, request)
        )
        await asyncio.wait_for(disconnect_probe.wait(), timeout=1)
        reading.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reading

        await asyncio.wait_for(slots.acquire(), timeout=0.1)
        slots.release()
        self.service.result.assert_not_awaited()

    async def test_result_disconnect_waits_for_fetch_cancellation_cleanup(self):
        succeeded = self.job.__class__(
            **{
                **self.job.__dict__,
                "status": "succeeded",
                "payload": None,
                "artifact_key": "slow-external-key",
                "media_type": "image/png",
                "filename": "capture.png",
                "artifact_bytes": 3,
                "result_expires_at": datetime.now(UTC) + timedelta(minutes=5),
                "completed_at": datetime.now(UTC),
            }
        )
        self.service.get.return_value = succeeded
        fetch_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def cancellation_resistant_result(_job):
            fetch_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_cleanup.wait()
                raise

        self.service.result.side_effect = cancellation_resistant_result
        slots = asyncio.Semaphore(1)
        main.app.state.async_result_slots = slots

        async def is_disconnected():
            return fetch_started.is_set()

        reading = asyncio.create_task(
            main.read_render_job_result(
                self.job.id,
                SimpleNamespace(is_disconnected=is_disconnected),
            )
        )
        await asyncio.wait_for(fetch_started.wait(), timeout=1)
        slot_waiter = asyncio.create_task(slots.acquire())
        await asyncio.sleep(0.15)
        self.assertFalse(reading.done())
        self.assertFalse(slot_waiter.done())

        release_cleanup.set()
        with self.assertRaises(RenderError) as error:
            await asyncio.wait_for(reading, timeout=1)
        self.assertEqual(error.exception.code, "client_disconnected")
        await asyncio.wait_for(slot_waiter, timeout=1)
        slots.release()

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
            await main.read_render_job_result(self.job.id, self.request)
        self.assertEqual(error.exception.code, "async_result_expired")
        self.assertEqual(error.exception.status_code, 410)

    async def test_async_render_timing_includes_capture_slot_wait(self):
        original_slots = getattr(main.app.state, "capture_slots", None)
        original_browser = getattr(main.app.state, "browser", None)
        main.app.state.capture_slots = asyncio.Semaphore(0)
        main.app.state.browser = SimpleNamespace()
        try:
            with (
                patch("main.time.perf_counter", side_effect=[10.0, 10.25])
                as clock,
                patch("main.RenderEngine") as engine_class,
            ):
                engine_class.return_value.render_image = AsyncMock(
                    return_value=SimpleNamespace(
                        body=b"png",
                        media_type="image/png",
                        filename="capture.png",
                    )
                )
                rendering = asyncio.create_task(
                    main._render_async_image(_payload())
                )
                await asyncio.sleep(0)
                self.assertEqual(clock.call_count, 1)
                main.app.state.capture_slots.release()
                rendered = await asyncio.wait_for(rendering, timeout=1)
            self.assertEqual(rendered.render_ms, 250)
        finally:
            main.app.state.capture_slots = original_slots
            main.app.state.browser = original_browser

    async def test_provider_close_error_does_not_skip_browser_cleanup(self):
        provider_error = RuntimeError("provider close failed")
        service = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(side_effect=provider_error),
        )
        browser = SimpleNamespace(close=AsyncMock())
        playwright = SimpleNamespace(stop=AsyncMock())
        manager = SimpleNamespace(start=AsyncMock(return_value=playwright))
        test_app = SimpleNamespace(state=SimpleNamespace())

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("main.async_playwright", return_value=manager),
                patch("main._launch_browser", AsyncMock(return_value=browser)),
                patch("main._detect_hardware_gpu", AsyncMock(return_value=False)),
                patch("main.ASYNC_JOBS_ENABLED", True),
                patch("main.ASYNC_JOB_SETTINGS", _settings(Path("."))),
                patch("main.CACHE_DIRECTORY", Path(directory) / "cache"),
                patch("main.load_providers", return_value=(object(), object())),
                patch("main.AsyncJobService", return_value=service),
            ):
                with self.assertRaisesRegex(RuntimeError, "provider close failed"):
                    async with main.lifespan(test_app):
                        pass

        service.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    async def test_partially_started_job_store_is_closed(self):
        job_store = SimpleNamespace(
            start=AsyncMock(side_effect=RuntimeError("start failed")),
            close=AsyncMock(),
        )
        artifact_store = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(),
        )
        service = AsyncJobService(
            _settings(Path(".")),
            job_store,
            artifact_store,
            _successful_renderer,
            cipher=PayloadCipher(b"\0" * 32),
        )

        with self.assertRaisesRegex(RuntimeError, "start failed"):
            await service.start()

        job_store.close.assert_awaited_once()
        artifact_store.start.assert_not_awaited()
        artifact_store.close.assert_not_awaited()

    async def test_start_failure_bounds_partially_started_provider_cleanup(self):
        never_closed = asyncio.Event()

        async def never_close():
            await never_closed.wait()

        job_store = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(side_effect=never_close),
            requeue_running=AsyncMock(),
            maintain=AsyncMock(return_value=[]),
        )
        artifact_store = SimpleNamespace(
            start=AsyncMock(side_effect=RuntimeError("artifact start failed")),
            close=AsyncMock(side_effect=never_close),
        )
        service = AsyncJobService(
            _settings(Path(".")),
            job_store,
            artifact_store,
            _successful_renderer,
            cipher=PayloadCipher(b"\0" * 32),
        )

        with (
            patch("async_jobs.asyncio.wait", wraps=asyncio.wait) as wait,
            self.assertRaisesRegex(RuntimeError, "artifact start failed"),
        ):
            await service.start()

        self.assertEqual(wait.await_count, 2)
        artifact_store.close.assert_awaited_once()
        job_store.close.assert_awaited_once()

    def test_openapi_exposes_complete_job_surface(self):
        paths = main.app.openapi()["paths"]
        self.assertIn("/v1/jobs", paths)
        self.assertIn("/v1/jobs/{job_id}", paths)
        self.assertIn("/v1/jobs/{job_id}/result", paths)

    def test_desktop_cors_allows_idempotency_header(self):
        self.assertIn("X-Request-Id", main.DESKTOP_ALLOW_HEADERS)
