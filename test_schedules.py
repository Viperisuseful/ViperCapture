import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from async_jobs import PayloadCipher
from render_contract import RenderRequest
from render_errors import RenderError
from schedules import (
    ScheduleCreate,
    ScheduleService,
    ScheduleStore,
    ScheduleUpdate,
    next_run,
    public_schedule_document,
    schedule_cursor,
    validate_cron,
)

UTC = timezone.utc


class FakeJobs:
    def __init__(self) -> None:
        self.calls = []

    async def submit(self, payload, *, request_id):
        self.calls.append((payload, request_id))
        return SimpleNamespace(
            id=f"job-{len(self.calls)}", status="queued", attempt_count=0
        )


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_bundled_store_refuses_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ScheduleStore(Path(directory) / "schedules.sqlite3")
            with (
                patch("schedules.os.name", "nt"),
                self.assertRaisesRegex(RuntimeError, "Windows ACLs"),
            ):
                await store.start()

    def test_cron_and_timezone_validation(self):
        validate_cron("*/5 * * * *", "America/New_York")
        with self.assertRaises(ValueError):
            validate_cron("* * *", "UTC")
        with self.assertRaises(ValueError):
            validate_cron("* * * * *", "Mars/Olympus")

    def test_next_run_is_utc(self):
        after = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        result = next_run("5 * * * *", "UTC", after)
        self.assertEqual(result, datetime(2026, 1, 1, 12, 5, tzinfo=UTC))

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(Path(self.temporary.name) / "schedules.sqlite3")
        self.jobs = FakeJobs()
        self.service = ScheduleService(
            self.store,
            self.jobs,
            PayloadCipher(b"s" * 32),
            poll_seconds=60,
        )
        await self.store.start()

    async def asyncTearDown(self):
        await self.store.close()
        self.temporary.cleanup()

    async def test_crud_hides_encrypted_payload(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Home page",
                cron="0 * * * *",
                render=RenderRequest(url="https://example.com"),
            )
        )
        raw = self.store.path.read_bytes()
        self.assertNotIn(b"https://example.com", raw)
        document = public_schedule_document(record)
        self.assertNotIn("payload", document)
        self.assertNotIn("render", document)

        updated = await self.service.update(
            record,
            ScheduleUpdate(
                name="Updated",
                enabled=False,
                render=RenderRequest(html="replacement"),
            ),
        )
        self.assertEqual(updated.name, "Updated")
        self.assertFalse(updated.enabled)
        database_files = (
            self.store.path,
            Path(f"{self.store.path}-wal"),
            Path(f"{self.store.path}-shm"),
        )
        history = b"".join(
            path.read_bytes() for path in database_files if path.exists()
        )
        self.assertNotIn(record.payload, history)
        self.assertEqual(len(await self.store.list()), 1)
        self.assertTrue(await self.store.delete(record.id))
        self.assertIsNone(await self.store.get(record.id))
        history = b"".join(
            path.read_bytes() for path in database_files if path.exists()
        )
        self.assertNotIn(updated.payload, history)

    async def test_sqlite_operations_run_off_the_event_loop(self):
        def run_inline(operation, *args):
            return operation(*args)

        with patch(
            "schedules.asyncio.to_thread",
            AsyncMock(side_effect=run_inline),
        ) as to_thread:
            self.assertEqual(await self.store.list(), [])
        to_thread.assert_awaited_once()

    async def test_list_is_paginated_without_loading_payloads(self):
        for index in range(3):
            await self.service.create(
                ScheduleCreate(
                    name=f"Schedule {index}",
                    cron="0 * * * *",
                    render=RenderRequest(html=f"secret-{index}"),
                )
            )
        statements = []
        self.store.connection.set_trace_callback(statements.append)
        first = await self.store.list(limit=2)
        cursor = schedule_cursor(first[-1])
        self.assertTrue(await self.store.delete(first[-1].id))
        second = await self.store.list(limit=2, after=cursor)
        self.store.connection.set_trace_callback(None)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 1)
        list_queries = [
            statement.lower()
            for statement in statements
            if "order by created_at" in statement.lower()
        ]
        self.assertTrue(list_queries)
        self.assertTrue(
            all(
                "payload" not in statement and "select *" not in statement
                for statement in list_queries
            )
        )

    async def test_list_filters_projects_inside_sqlite(self):
        for project_id in ("project-a", "project-b"):
            for index in range(2):
                await self.service.create(
                    ScheduleCreate(
                        name=f"{project_id}-{index}",
                        cron="0 * * * *",
                        render=RenderRequest(html=project_id),
                    ),
                    project_id=project_id,
                )
        statements = []
        self.store.connection.set_trace_callback(statements.append)
        records = await self.store.list(project_id="project-b")
        self.store.connection.set_trace_callback(None)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.name.startswith("project-b") for record in records))
        queries = [statement for statement in statements if "FROM schedules" in statement]
        self.assertEqual(len(queries), 1)
        self.assertIn("project_id=", queries[0])

    async def test_update_conflicts_with_concurrent_scheduler_advance(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Race",
                cron="* * * * *",
                render=RenderRequest(html="race"),
            )
        )
        due = datetime.now(UTC) + timedelta(minutes=2)
        await self.store.claim_due(due)
        advanced = await self.store.get(record.id)
        with self.assertRaises(RenderError) as raised:
            await self.service.update(record, ScheduleUpdate(name="Stale"))
        self.assertEqual(raised.exception.code, "schedule_conflict")
        stored = await self.store.get(record.id)
        self.assertEqual(stored.next_run_at, advanced.next_run_at)

    async def test_invalid_partial_schedule_update_is_typed(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Validation",
                cron="0 * * * *",
                render=RenderRequest(html="valid"),
            )
        )
        with self.assertRaises(RenderError) as raised:
            await self.service.update(
                record, ScheduleUpdate(cron="not a cron")
            )
        self.assertEqual(raised.exception.code, "invalid_schedule")
        self.assertEqual(raised.exception.status_code, 422)

    async def test_committed_update_defers_busy_payload_scrub(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Scrub retry",
                cron="0 * * * *",
                render=RenderRequest(html="old"),
            )
        )
        with patch.object(
            self.store,
            "_scrub_payload_history",
            side_effect=(RuntimeError("busy"), None),
        ) as scrub:
            updated = await self.service.update(
                record,
                ScheduleUpdate(render=RenderRequest(html="new")),
            )
            self.assertEqual(updated.name, "Scrub retry")
            self.assertTrue(self.store._scrub_pending)
            self.assertIsNotNone(await self.store.get(record.id))
            self.assertEqual(scrub.call_count, 2)
            self.assertFalse(self.store._scrub_pending)

    async def test_due_schedule_submits_once_and_advances_first(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Minute",
                cron="* * * * *",
                render=RenderRequest(html="<h1>scheduled</h1>"),
            )
        )
        due = datetime.now(UTC) + timedelta(minutes=2)
        self.assertEqual(await self.service.run_due(due), 1)
        self.assertEqual(len(self.jobs.calls), 1)
        payload, request_id = self.jobs.calls[0]
        self.assertEqual(payload.html, "<h1>scheduled</h1>")
        self.assertTrue(request_id.startswith(f"_schedule-{record.id}-"))
        stored = await self.store.get(record.id)
        self.assertEqual(stored.last_job_id, "job-1")
        self.assertGreater(stored.next_run_at, due)
        self.assertEqual(await self.service.run_due(due), 0)

    async def test_project_schedule_prefix_reaches_the_worker(self):
        project_id = "a" * 24
        self.service.project_for_schedule = AsyncMock(return_value=project_id)
        record = await self.service.create(
            ScheduleCreate(
                name="Owned",
                cron="* * * * *",
                render=RenderRequest(html="owned"),
            )
        )
        await self.service.run_due(datetime.now(UTC) + timedelta(minutes=2))
        self.assertTrue(
            self.jobs.calls[0][1].startswith(
                f"_project-{project_id}:_schedule-{record.id}-"
            )
        )

    async def test_internal_request_ids_use_reserved_namespace(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Reserved ID",
                cron="* * * * *",
                render=RenderRequest(html="scheduled"),
            )
        )

        await self.service.run_due(datetime.now(UTC) + timedelta(minutes=2))

        request_id = self.jobs.calls[0][1]
        self.assertTrue(request_id.startswith("_schedule-"))
        self.assertRegex(request_id, r"^_[A-Za-z0-9._:-]+$")

    async def test_retryable_submission_failure_preserves_due_occurrence(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Retry",
                cron="* * * * *",
                render=RenderRequest(html="retry me"),
            )
        )
        due = datetime.now(UTC) + timedelta(minutes=2)
        successful_submit = self.jobs.submit
        self.jobs.submit = AsyncMock(
            side_effect=RenderError(
                "async_queue_full", "Queue full", 503, True
            )
        )

        self.assertEqual(await self.service.run_due(due), 1)
        failed = await self.store.get(record.id)
        self.assertEqual(failed.last_error, "RenderError")
        request_id = self.jobs.submit.await_args.kwargs["request_id"]

        self.jobs.submit = successful_submit
        self.assertEqual(
            await self.service.run_due(due + timedelta(seconds=1)), 1
        )
        self.assertEqual(self.jobs.calls[-1][1], request_id)
        retried = await self.store.get(record.id)
        self.assertEqual(retried.last_job_id, "job-1")

    async def test_ownership_is_committed_before_occurrence_advances(self):
        ownership = AsyncMock(side_effect=(RuntimeError("database busy"), None))
        self.service.on_job_created = ownership
        record = await self.service.create(
            ScheduleCreate(
                name="Ownership retry",
                cron="* * * * *",
                render=RenderRequest(html="owned"),
            )
        )
        due = datetime.now(UTC) + timedelta(minutes=2)

        self.assertEqual(await self.service.run_due(due), 1)
        failed = await self.store.get(record.id)
        self.assertIsNone(failed.last_job_id)
        request_id = self.jobs.calls[0][1]

        self.assertEqual(
            await self.service.run_due(due + timedelta(seconds=1)), 1
        )
        self.assertEqual(self.jobs.calls[1][1], request_id)
        ownership.assert_awaited_with(record.id, "job-2")
        completed = await self.store.get(record.id)
        self.assertEqual(completed.last_job_id, "job-2")

    async def test_claimed_occurrence_survives_store_restart(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Crash recovery",
                cron="* * * * *",
                render=RenderRequest(html="recover me"),
            )
        )
        now = datetime.now(UTC) + timedelta(minutes=2)
        claimed = await self.store.claim_due(now)
        due_at = claimed[0][1]
        await self.store.close()

        self.store = ScheduleStore(
            Path(self.temporary.name) / "schedules.sqlite3"
        )
        await self.store.start()
        recovered = ScheduleService(
            self.store,
            self.jobs,
            PayloadCipher(b"s" * 32),
            poll_seconds=60,
        )
        self.assertEqual(
            await recovered.run_due(now + timedelta(seconds=1)), 1
        )
        self.assertEqual(
            self.jobs.calls[-1][1],
            f"_schedule-{record.id}-{int(due_at.timestamp())}",
        )

    async def test_expired_unstarted_job_rotates_durable_request_id(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Expired recovery",
                cron="* * * * *",
                render=RenderRequest(html="retry expired"),
            )
        )
        now = datetime.now(UTC) + timedelta(minutes=2)
        successful_submit = self.jobs.submit
        self.jobs.submit = AsyncMock(
            return_value=SimpleNamespace(
                id="expired-job", status="expired", attempt_count=0
            )
        )
        await self.service.run_due(now)
        original_id = self.jobs.submit.await_args.kwargs["request_id"]

        self.jobs.submit = successful_submit
        await self.service.run_due(now + timedelta(seconds=1))
        self.assertEqual(self.jobs.calls[-1][1], f"{original_id}-retry-1")
        stored = await self.store.get(record.id)
        self.assertEqual(stored.last_job_id, "job-1")

    async def test_deleted_claim_is_revalidated_before_submission(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Delete race",
                cron="* * * * *",
                render=RenderRequest(html="do not submit"),
            )
        )
        now = datetime.now(UTC) + timedelta(minutes=2)
        claimed = await self.store.claim_due(now)
        self.assertTrue(await self.service.delete(record.id))
        self.store.claim_due = AsyncMock(return_value=claimed)

        await self.service.run_due(now)

        self.assertEqual(self.jobs.calls, [])

    async def test_claim_reloads_payload_changed_by_update(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Update race",
                cron="* * * * *",
                render=RenderRequest(html="old payload"),
            )
        )
        now = datetime.now(UTC) + timedelta(minutes=2)
        claimed = await self.store.claim_due(now)
        current = await self.store.get(record.id)
        await self.service.update(
            current,
            ScheduleUpdate(render=RenderRequest(html="new payload")),
        )
        self.store.claim_due = AsyncMock(return_value=claimed)

        await self.service.run_due(now)

        self.assertEqual(self.jobs.calls[0][0].html, "new payload")

    async def test_disabling_schedule_clears_pending_retry_counter(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Disable retry",
                cron="* * * * *",
                render=RenderRequest(html="pending"),
            )
        )
        now = datetime.now(UTC) + timedelta(minutes=2)
        due_at = (await self.store.claim_due(now))[0][1]
        self.assertTrue(
            await self.store.advance_occurrence_attempt(
                record.id,
                due_at=due_at,
                expected_attempt=0,
            )
        )
        pending = await self.store.get(record.id)
        self.assertEqual(pending.pending_attempt, 1)

        await self.service.update(pending, ScheduleUpdate(enabled=False))

        disabled = await self.store.get(record.id)
        self.assertEqual(disabled.pending_attempt, 0)

    async def test_reenabling_skips_occurrences_missed_while_disabled(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Resume future",
                cron="* * * * *",
                enabled=False,
                render=RenderRequest(html="future"),
            )
        )
        stale = replace(
            record,
            next_run_at=datetime.now(UTC) - timedelta(minutes=5),
            updated_at=datetime.now(UTC),
        )
        await self.store.update(stale, expected_updated_at=record.updated_at)

        resumed = await self.service.update(
            stale, ScheduleUpdate(enabled=True)
        )

        self.assertGreater(resumed.next_run_at, datetime.now(UTC))


if __name__ == "__main__":
    unittest.main()
