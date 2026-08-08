import tempfile
import unittest
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
    validate_cron,
)

UTC = timezone.utc


class FakeJobs:
    def __init__(self) -> None:
        self.calls = []

    async def submit(self, payload, *, request_id):
        self.calls.append((payload, request_id))
        return SimpleNamespace(id=f"job-{len(self.calls)}")


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
            ScheduleUpdate(name="Updated", enabled=False),
        )
        self.assertEqual(updated.name, "Updated")
        self.assertFalse(updated.enabled)
        self.assertEqual(len(await self.store.list()), 1)
        self.assertTrue(await self.store.delete(record.id))
        self.assertIsNone(await self.store.get(record.id))

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
        second = await self.store.list(limit=2, after=first[-1].id)
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
        self.assertTrue(request_id.startswith(f"schedule-{record.id}-"))
        stored = await self.store.get(record.id)
        self.assertEqual(stored.last_job_id, "job-1")
        self.assertGreater(stored.next_run_at, due)
        self.assertEqual(await self.service.run_due(due), 0)


if __name__ == "__main__":
    unittest.main()
