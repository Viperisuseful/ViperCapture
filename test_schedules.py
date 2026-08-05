import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from async_jobs import PayloadCipher
from render_contract import RenderRequest
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

    async def test_update_preserves_scheduler_advancement(self):
        record = await self.service.create(
            ScheduleCreate(
                name="Hourly",
                cron="0 * * * *",
                render=RenderRequest(html="<h1>scheduled</h1>"),
            )
        )
        # A stale snapshot taken before the scheduler claims the schedule.
        stale = await self.store.get(record.id)
        assert stale is not None
        due = datetime.now(UTC) + timedelta(hours=2)
        self.assertEqual(await self.service.run_due(due), 1)
        claimed = await self.store.get(record.id)
        assert claimed is not None
        self.assertGreater(claimed.next_run_at, stale.next_run_at)
        self.assertEqual(claimed.last_job_id, "job-1")

        # Updating name/payload from the stale snapshot must not restore the
        # pre-claim due timestamp or erase the recorded result.
        updated = await self.service.update(
            stale,
            ScheduleUpdate(name="Renamed", render=RenderRequest(html="<p>new</p>")),
        )
        self.assertEqual(updated.name, "Renamed")
        self.assertEqual(updated.next_run_at, claimed.next_run_at)
        self.assertEqual(updated.last_job_id, "job-1")
        self.assertEqual(await self.service.run_due(due), 0)

        # Changing the clock still recomputes the next occurrence.
        current = await self.store.get(record.id)
        assert current is not None
        moved = await self.service.update(
            current,
            ScheduleUpdate(cron="30 3 * * *"),
        )
        self.assertNotEqual(moved.next_run_at, claimed.next_run_at)

    async def test_schedule_listing_pages_without_payloads(self):
        for index in range(3):
            await self.service.create(
                ScheduleCreate(
                    name=f"Job {index}",
                    cron="0 * * * *",
                    render=RenderRequest(html=f"<h1>{index}</h1>"),
                )
            )
        first = await self.store.list_page(limit=2)
        self.assertEqual(len(first), 2)
        self.assertNotIn("payload", first[0])
        self.assertNotIn("render", first[0])
        second = await self.store.list_page(after=str(first[-1]["id"]), limit=2)
        self.assertEqual(len(second), 1)
        self.assertEqual(
            {item["id"] for item in first + second},
            {record.id for record in await self.store.list()},
        )


if __name__ == "__main__":
    unittest.main()
