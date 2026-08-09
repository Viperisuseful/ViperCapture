"""Durable cron schedules backed by encrypted render payloads and SQLite."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sqlite3
import stat
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from async_jobs import AsyncJobService, PayloadCipher, _ensure_private_directory
from render_contract import RenderRequest
from render_errors import RenderError

UTC = timezone.utc
logger = logging.getLogger("vipercapture.schedules")


def load_schedule_store(settings) -> "ScheduleStore":
    spec = os.getenv("VIPERCAPTURE_SCHEDULE_STORE_FACTORY", "")
    if not spec:
        return ScheduleStore(settings.data_dir / "schedules.sqlite3")
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(
            "VIPERCAPTURE_SCHEDULE_STORE_FACTORY must use module:function syntax"
        )
    return getattr(importlib.import_module(module_name), attribute)(settings)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleCreate(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    cron: str = Field(min_length=1, max_length=128)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    enabled: bool = True
    render: RenderRequest

    @model_validator(mode="after")
    def validate_schedule(self) -> "ScheduleCreate":
        validate_cron(self.cron, self.timezone)
        return self


class ScheduleUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    cron: str | None = Field(default=None, min_length=1, max_length=128)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    render: RenderRequest | None = None


@dataclass(frozen=True)
class ScheduleRecord:
    id: str
    name: str
    cron: str
    timezone: str
    enabled: bool
    payload: bytes
    next_run_at: datetime
    created_at: datetime
    updated_at: datetime
    last_run_at: datetime | None = None
    last_job_id: str | None = None
    last_error: str | None = None
    pending_attempt: int = 0
    project_id: str | None = None


def validate_cron(expression: str, timezone_name: str) -> None:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA time zone") from exc
    if not croniter.is_valid(expression):
        raise ValueError("cron must be a valid five-field cron expression")
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron must use exactly five fields")
    croniter(expression, datetime.now(zone)).get_next(datetime)


def next_run(expression: str, timezone_name: str, after: datetime) -> datetime:
    zone = ZoneInfo(timezone_name)
    localized = after.astimezone(zone)
    return croniter(expression, localized).get_next(datetime).astimezone(UTC)


def _as_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def schedule_cursor(record: ScheduleRecord) -> str:
    value = f"{record.created_at.isoformat()}\0{record.id}".encode("utf-8")
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def parse_schedule_cursor(value: str) -> tuple[str, str]:
    try:
        decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        created_at, separator, schedule_id = decoded.partition("\0")
        datetime.fromisoformat(created_at)
        UUID(schedule_id)
        if not separator or not schedule_id:
            raise ValueError
        return created_at, schedule_id
    except (BinasciiError, ValueError, UnicodeDecodeError) as exc:
        raise RenderError(
            "invalid_schedule_cursor",
            "The schedule cursor is invalid.",
            422,
            False,
        ) from exc


class ScheduleStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None
        self.lock = asyncio.Lock()
        self._scrub_pending = False

    async def start(self) -> None:
        if os.name == "nt":
            raise RuntimeError(
                "the bundled schedule store cannot enforce private Windows ACLs; "
                "set VIPERCAPTURE_SCHEDULES=0"
            )
        await asyncio.to_thread(self._start)

    def _start(self) -> None:
        _ensure_private_directory(self.path.parent)
        existed = self.path.exists()
        if existed and os.name != "nt":
            information = self.path.lstat()
            if not stat.S_ISREG(information.st_mode) or information.st_mode & 0o077:
                raise RuntimeError("schedule database must be an owner-only regular file")
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        if not existed and os.name != "nt":
            os.chmod(self.path, 0o600)
        if os.name != "nt":
            information = self.path.stat()
            if not stat.S_ISREG(information.st_mode) or information.st_mode & 0o077:
                raise RuntimeError("schedule database must be an owner-only regular file")
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                cron TEXT NOT NULL,
                timezone TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                payload BLOB NOT NULL,
                next_run_at TEXT NOT NULL,
                pending_run_at TEXT,
                pending_attempt INTEGER NOT NULL DEFAULT 0,
                last_run_at TEXT,
                last_job_id TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                project_id TEXT
            );
            CREATE INDEX IF NOT EXISTS schedules_due_idx
                ON schedules(enabled, next_run_at);
            """
        )
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(schedules)")
        }
        if "pending_run_at" not in columns:
            self.connection.execute(
                "ALTER TABLE schedules ADD COLUMN pending_run_at TEXT"
            )
        if "pending_attempt" not in columns:
            self.connection.execute(
                "ALTER TABLE schedules ADD COLUMN pending_attempt INTEGER NOT NULL DEFAULT 0"
            )
        if "project_id" not in columns:
            self.connection.execute("ALTER TABLE schedules ADD COLUMN project_id TEXT")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS schedules_project_list_idx "
            "ON schedules(project_id, created_at, id)"
        )
        self.connection.commit()
        self._scrub_pending = existed
        self._try_scrub_payload_history()
        if os.name != "nt":
            for candidate in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            ):
                if candidate.exists():
                    information = candidate.lstat()
                    if not stat.S_ISREG(information.st_mode):
                        raise RuntimeError("schedule database files must be regular files")
                    os.chmod(candidate, 0o600)

    async def close(self) -> None:
        async with self.lock:
            await asyncio.to_thread(self._close)

    def _close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    async def _run(self, operation, *args):
        async with self.lock:
            def execute():
                result = operation(*args)
                self._try_scrub_payload_history()
                return result

            task = asyncio.create_task(asyncio.to_thread(execute))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.shield(task)
                raise

    def _require(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("schedule store is not started")
        return self.connection

    @staticmethod
    def _record(row: sqlite3.Row) -> ScheduleRecord:
        columns = row.keys()
        return ScheduleRecord(
            id=row["id"],
            name=row["name"],
            cron=row["cron"],
            timezone=row["timezone"],
            enabled=bool(row["enabled"]),
            payload=bytes(row["payload"]) if "payload" in columns else b"",
            next_run_at=_as_utc(row["next_run_at"]),
            last_run_at=_as_utc(row["last_run_at"]),
            last_job_id=row["last_job_id"],
            last_error=row["last_error"],
            created_at=_as_utc(row["created_at"]),
            updated_at=_as_utc(row["updated_at"]),
            pending_attempt=(
                int(row["pending_attempt"])
                if "pending_attempt" in columns
                else 0
            ),
            project_id=(
                str(row["project_id"])
                if "project_id" in columns and row["project_id"] is not None
                else None
            ),
        )

    async def create(self, record: ScheduleRecord) -> ScheduleRecord:
        await self._run(self._create, record)
        return record

    def _create(self, record: ScheduleRecord) -> None:
        connection = self._require()
        connection.execute(
            """INSERT INTO schedules
            (id,name,cron,timezone,enabled,payload,next_run_at,last_run_at,
             last_job_id,last_error,created_at,updated_at,project_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.id,
                record.name,
                record.cron,
                record.timezone,
                int(record.enabled),
                record.payload,
                record.next_run_at.isoformat(),
                None,
                None,
                None,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
                record.project_id,
            ),
        )
        connection.commit()

    async def get(self, schedule_id: str) -> ScheduleRecord | None:
        row = await self._run(self._get, schedule_id)
        return self._record(row) if row else None

    def _get(self, schedule_id: str) -> sqlite3.Row | None:
        return self._require().execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
        ).fetchone()

    async def list(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        project_id: str | None = None,
    ) -> list[ScheduleRecord]:
        rows = await self._run(self._list, limit, after, project_id)
        return [self._record(row) for row in rows]

    def _list(
        self, limit: int, after: str | None, project_id: str | None
    ) -> list[sqlite3.Row]:
        connection = self._require()
        cursor_created_at = None
        cursor_id = None
        if after is not None:
            cursor_created_at, cursor_id = parse_schedule_cursor(after)
        columns = (
            "id,name,cron,timezone,enabled,next_run_at,last_run_at,last_job_id,"
            "last_error,created_at,updated_at"
        )
        if cursor_created_at is None:
            if project_id is not None:
                return connection.execute(
                    f"SELECT {columns} FROM schedules WHERE project_id=? "
                    "ORDER BY created_at,id LIMIT ?",
                    (project_id, limit),
                ).fetchall()
            return connection.execute(
                f"SELECT {columns} FROM schedules ORDER BY created_at,id LIMIT ?",
                (limit,),
            ).fetchall()
        project_filter = "project_id=? AND " if project_id is not None else ""
        parameters = (
            (project_id, cursor_created_at, cursor_created_at, cursor_id, limit)
            if project_id is not None
            else (cursor_created_at, cursor_created_at, cursor_id, limit)
        )
        return connection.execute(
            f"SELECT {columns} FROM schedules WHERE {project_filter}"
            "(created_at > ? OR (created_at = ? AND id > ?)) "
            "ORDER BY created_at,id LIMIT ?",
            parameters,
        ).fetchall()

    async def update(
        self, record: ScheduleRecord, *, expected_updated_at: datetime
    ) -> ScheduleRecord:
        if not await self._run(self._update, record, expected_updated_at):
            raise KeyError(record.id)
        return record

    def _update(self, record: ScheduleRecord, expected_updated_at: datetime) -> bool:
        connection = self._require()
        cursor = connection.execute(
            """UPDATE schedules SET name=?,cron=?,timezone=?,enabled=?,payload=?,
            next_run_at=?,pending_run_at=CASE WHEN ?=0 THEN NULL ELSE pending_run_at END,
            pending_attempt=CASE WHEN ?=0 THEN 0 ELSE pending_attempt END,
            updated_at=? WHERE id=? AND updated_at=?""",
            (
                record.name,
                record.cron,
                record.timezone,
                int(record.enabled),
                record.payload,
                record.next_run_at.isoformat(),
                int(record.enabled),
                int(record.enabled),
                record.updated_at.isoformat(),
                record.id,
                expected_updated_at.isoformat(),
            ),
        )
        connection.commit()
        if cursor.rowcount:
            self._scrub_pending = True
        return cursor.rowcount == 1

    async def delete(self, schedule_id: str) -> bool:
        return await self._run(self._delete, schedule_id)

    def _delete(self, schedule_id: str) -> bool:
        connection = self._require()
        cursor = connection.execute(
            "DELETE FROM schedules WHERE id = ?", (schedule_id,)
        )
        connection.commit()
        if cursor.rowcount:
            self._scrub_pending = True
        return cursor.rowcount == 1

    def _try_scrub_payload_history(self) -> None:
        if not self._scrub_pending or self.connection is None:
            return
        try:
            self._scrub_payload_history(self.connection)
        except RuntimeError:
            logger.warning("schedule payload scrub checkpoint busy; retrying later")
        else:
            self._scrub_pending = False

    @staticmethod
    def _scrub_payload_history(connection: sqlite3.Connection) -> None:
        checkpoint = connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if checkpoint is not None and checkpoint[0]:
            raise RuntimeError("schedule payload scrub checkpoint was busy")

    async def claim_due(self, now: datetime) -> list[tuple[ScheduleRecord, datetime]]:
        return await self._run(self._claim_due, now)

    async def claim_is_current(
        self,
        schedule_id: str,
        due_at: datetime,
        pending_attempt: int,
    ) -> bool:
        return await self._run(
            self._claim_is_current,
            schedule_id,
            due_at,
            pending_attempt,
        )

    def _claim_is_current(
        self,
        schedule_id: str,
        due_at: datetime,
        pending_attempt: int,
    ) -> bool:
        row = self._require().execute(
            """SELECT 1 FROM schedules WHERE id=? AND enabled=1
            AND pending_run_at=? AND pending_attempt=?""",
            (schedule_id, due_at.isoformat(), pending_attempt),
        ).fetchone()
        return row is not None

    def _claim_due(
        self, now: datetime
    ) -> list[tuple[ScheduleRecord, datetime]]:
        claimed = []
        connection = self._require()
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                "SELECT * FROM schedules WHERE pending_run_at IS NOT NULL OR "
                "(enabled = 1 AND next_run_at <= ?) "
                "ORDER BY COALESCE(pending_run_at,next_run_at) LIMIT 100",
                (now.isoformat(),),
            ).fetchall()
            for row in rows:
                record = self._record(row)
                pending_run_at = _as_utc(row["pending_run_at"])
                due_at = pending_run_at or record.next_run_at
                if pending_run_at is not None:
                    claimed.append((record, due_at))
                    continue
                following = next_run(record.cron, record.timezone, now)
                cursor = connection.execute(
                    """UPDATE schedules SET next_run_at=?,pending_run_at=?,pending_attempt=0,last_run_at=?,
                    last_error=NULL,updated_at=? WHERE id=? AND next_run_at=?""",
                    (
                        following.isoformat(),
                        due_at.isoformat(),
                        due_at.isoformat(),
                        now.isoformat(),
                        record.id,
                        due_at.isoformat(),
                    ),
                )
                if cursor.rowcount == 1:
                    claimed.append((record, due_at))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return claimed

    async def record_result(
        self,
        schedule_id: str,
        *,
        job_id: str | None,
        error: str | None,
    ) -> None:
        await self._run(
            self._record_result,
            schedule_id,
            job_id,
            error,
        )

    def _record_result(
        self,
        schedule_id: str,
        job_id: str | None,
        error: str | None,
    ) -> None:
        connection = self._require()
        connection.execute(
            """UPDATE schedules SET pending_run_at=NULL,pending_attempt=0,last_job_id=?,last_error=?,
            updated_at=? WHERE id=?""",
            (job_id, error, datetime.now(UTC).isoformat(), schedule_id),
        )
        connection.commit()

    async def retry_occurrence(
        self,
        schedule_id: str,
        *,
        due_at: datetime,
        error: str,
    ) -> bool:
        return await self._run(
            self._retry_occurrence,
            schedule_id,
            due_at,
            error,
        )

    def _retry_occurrence(
        self,
        schedule_id: str,
        due_at: datetime,
        error: str,
    ) -> bool:
        connection = self._require()
        cursor = connection.execute(
            """UPDATE schedules SET last_error=?,updated_at=?
            WHERE id=? AND pending_run_at=?""",
            (
                error,
                datetime.now(UTC).isoformat(),
                schedule_id,
                due_at.isoformat(),
            ),
        )
        connection.commit()
        return cursor.rowcount == 1

    async def advance_occurrence_attempt(
        self,
        schedule_id: str,
        *,
        due_at: datetime,
        expected_attempt: int,
    ) -> bool:
        return await self._run(
            self._advance_occurrence_attempt,
            schedule_id,
            due_at,
            expected_attempt,
        )

    def _advance_occurrence_attempt(
        self,
        schedule_id: str,
        due_at: datetime,
        expected_attempt: int,
    ) -> bool:
        connection = self._require()
        cursor = connection.execute(
            """UPDATE schedules SET pending_attempt=pending_attempt+1,updated_at=?
            WHERE id=? AND pending_run_at=? AND pending_attempt=?""",
            (
                datetime.now(UTC).isoformat(),
                schedule_id,
                due_at.isoformat(),
                expected_attempt,
            ),
        )
        connection.commit()
        return cursor.rowcount == 1


class ScheduleService:
    def __init__(
        self,
        store: ScheduleStore,
        jobs: AsyncJobService,
        cipher: PayloadCipher,
        *,
        poll_seconds: float = 1.0,
        on_job_created=None,
        project_for_schedule=None,
    ) -> None:
        self.store = store
        self.jobs = jobs
        self.cipher = cipher
        self.poll_seconds = max(0.1, poll_seconds)
        self.on_job_created = on_job_created
        self.project_for_schedule = project_for_schedule
        self.task: asyncio.Task | None = None
        self.mutation_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.store.start()
        self.task = asyncio.create_task(self._loop(), name="vipercapture-scheduler")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        await self.store.close()

    def _encrypt(self, schedule_id: str, request: RenderRequest) -> bytes:
        return self.cipher.encrypt(schedule_id, request)

    def _decrypt(self, record: ScheduleRecord) -> RenderRequest:
        return self.cipher.decrypt(SimpleNamespace(id=record.id, payload=record.payload))

    def payload_size(self, schedule_id: str, request: RenderRequest) -> int:
        return len(self._encrypt(schedule_id, request))

    async def create(
        self,
        request: ScheduleCreate,
        *,
        schedule_id: str | None = None,
        project_id: str | None = None,
    ) -> ScheduleRecord:
        now = datetime.now(UTC)
        schedule_id = schedule_id or str(uuid4())
        record = ScheduleRecord(
            id=schedule_id,
            name=request.name,
            cron=request.cron,
            timezone=request.timezone,
            enabled=request.enabled,
            payload=self._encrypt(schedule_id, request.render),
            next_run_at=next_run(request.cron, request.timezone, now),
            created_at=now,
            updated_at=now,
            project_id=project_id,
        )
        return await self.store.create(record)

    async def update(self, record: ScheduleRecord, request: ScheduleUpdate) -> ScheduleRecord:
        name = request.name if request.name is not None else record.name
        expression = request.cron if request.cron is not None else record.cron
        timezone_name = request.timezone if request.timezone is not None else record.timezone
        try:
            validate_cron(expression, timezone_name)
        except ValueError as exc:
            raise RenderError(
                "invalid_schedule",
                str(exc),
                422,
                False,
            ) from exc
        now = datetime.now(UTC)
        changed_clock = (
            request.cron is not None
            or request.timezone is not None
            or (request.enabled is True and not record.enabled)
        )
        updated = ScheduleRecord(
            **{
                **record.__dict__,
                "name": name,
                "cron": expression,
                "timezone": timezone_name,
                "enabled": request.enabled if request.enabled is not None else record.enabled,
                "payload": (
                    self._encrypt(record.id, request.render)
                    if request.render is not None
                    else record.payload
                ),
                "next_run_at": (
                    next_run(expression, timezone_name, now)
                    if changed_clock
                    else record.next_run_at
                ),
                "updated_at": now,
            }
        )
        try:
            async with self.mutation_lock:
                return await self.store.update(
                    updated, expected_updated_at=record.updated_at
                )
        except KeyError as exc:
            raise RenderError(
                "schedule_conflict",
                "The schedule changed while it was being updated.",
                409,
                True,
            ) from exc

    async def delete(self, schedule_id: str) -> bool:
        async with self.mutation_lock:
            return await self.store.delete(schedule_id)

    async def run_due(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        claimed = await self.store.claim_due(current)
        for record, due_at in claimed:
            try:
                async with self.mutation_lock:
                    current_record = await self.store.get(record.id)
                    if (
                        current_record is None
                        or current_record.pending_attempt != record.pending_attempt
                    ):
                        continue
                    if not await self.store.claim_is_current(
                        record.id,
                        due_at,
                        record.pending_attempt,
                    ):
                        continue
                    try:
                        render = self._decrypt(current_record)
                    except Exception as exc:
                        await self.store.record_result(
                            record.id,
                            job_id=None,
                            error=type(exc).__name__,
                        )
                        logger.warning(
                            "scheduled render payload invalid "
                            "schedule_id=%s error_type=%s",
                            record.id,
                            type(exc).__name__,
                        )
                        continue
                    request_id = (
                        f"_schedule-{record.id}-{int(due_at.timestamp())}"
                        + (
                            f"-retry-{record.pending_attempt}"
                            if record.pending_attempt
                            else ""
                        )
                    )
                    if self.project_for_schedule is not None:
                        project_id = await self.project_for_schedule(record.id)
                        if project_id is not None:
                            request_id = f"_project-{project_id}:{request_id}"
                    job = await self.jobs.submit(
                        render,
                        request_id=request_id,
                    )
                    if job.status == "expired" and job.attempt_count == 0:
                        await self.store.advance_occurrence_attempt(
                            record.id,
                            due_at=due_at,
                            expected_attempt=record.pending_attempt,
                        )
                        continue
                    if self.on_job_created is not None:
                        await self.on_job_created(record.id, job.id)
                    await self.store.record_result(
                        record.id, job_id=job.id, error=None
                    )
            except Exception as exc:
                retryable = not isinstance(exc, RenderError) or exc.retryable
                if retryable:
                    await self.store.retry_occurrence(
                        record.id,
                        due_at=due_at,
                        error=type(exc).__name__,
                    )
                else:
                    await self.store.record_result(
                        record.id,
                        job_id=None,
                        error=type(exc).__name__,
                    )
                logger.warning(
                    "scheduled render submission failed schedule_id=%s error_type=%s",
                    record.id,
                    type(exc).__name__,
                )
        return len(claimed)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("schedule loop failed error_type=%s", type(exc).__name__)
            await asyncio.sleep(self.poll_seconds)


def public_schedule_document(record: ScheduleRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "name": record.name,
        "cron": record.cron,
        "timezone": record.timezone,
        "enabled": record.enabled,
        "next_run_at": record.next_run_at.isoformat(),
        "last_run_at": record.last_run_at.isoformat() if record.last_run_at else None,
        "last_job_id": record.last_job_id,
        "last_error": record.last_error,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }
