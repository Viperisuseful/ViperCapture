import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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
