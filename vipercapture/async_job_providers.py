"""Bundled SQLite job state and filesystem artifact providers."""

from __future__ import annotations

import asyncio
import errno
import hmac
import json
import os
import re
import sqlite3
import stat
import threading
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .async_jobs import (
    Artifact,
    ArtifactExpiredError,
    ArtifactStoreConfig,
    IdempotencyConflictError,
    JobConflictError,
    JobRecord,
    JobStoreConfig,
    QueueFullError,
    StoredArtifact,
    _ensure_private_directory,
)

UTC = timezone.utc


def _epoch(value: datetime | None) -> float | None:
    return value.timestamp() if value is not None else None


def _datetime(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value is not None else None


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SQLiteJobStore:
    """Single-process durable queue using Python's bundled SQLite driver."""

    def __init__(self, config: JobStoreConfig) -> None:
        self.config = config
        self.path = config.data_dir / "async-jobs.sqlite3"
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._scrub_pending = False

    @staticmethod
    def _validate_sidecar(
        path: Path,
        *,
        secure_permissions: bool,
    ) -> None:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError(
                "async job database sidecar must be a private regular file"
            ) from exc
        try:
            information = os.fstat(descriptor)
            if (
                not stat.S_ISREG(information.st_mode)
                or information.st_uid != os.getuid()
            ):
                raise RuntimeError(
                    "async job database sidecar must be owner-controlled"
                )
            if secure_permissions:
                os.fchmod(descriptor, 0o600)
                information = os.fstat(descriptor)
            if stat.S_IMODE(information.st_mode) != 0o600:
                raise RuntimeError(
                    "async job database sidecar must be owner-only"
                )
        finally:
            os.close(descriptor)

    async def start(self) -> None:
        if os.name == "nt":
            raise RuntimeError(
                "The bundled SQLite job store requires POSIX owner-only "
                "permissions; configure an external job store on Windows."
            )
        _ensure_private_directory(self.config.data_dir)
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            descriptor = os.open(
                self.path,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            created = True
        except FileExistsError:
            try:
                descriptor = os.open(self.path, flags)
            except OSError as exc:
                raise RuntimeError(
                    "async job database must be a private regular file"
                ) from exc
        try:
            information = os.fstat(descriptor)
            if (
                not stat.S_ISREG(information.st_mode)
                or information.st_uid != os.getuid()
                or (
                    not created
                    and stat.S_IMODE(information.st_mode) != 0o600
                )
            ):
                raise RuntimeError(
                    "async job database must be an owner-only regular file"
                )
            if created:
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        if created:
            _sync_directory(self.config.data_dir)
        sidecars = (
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        )
        recovery_files = (
            *sidecars,
            Path(f"{self.path}-journal"),
        )
        for path in recovery_files:
            self._validate_sidecar(path, secure_permissions=False)
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._scrub_pending = not created
        await self._run(self._initialize)
        for path in (self.path, *recovery_files):
            self._validate_sidecar(path, secure_permissions=True)
        _sync_directory(self.config.data_dir)

    async def close(self) -> None:
        if self._connection is None:
            return
        async with self._lock:
            connection, self._connection = self._connection, None
            await asyncio.to_thread(connection.close)

    async def _run(
        self,
        operation,
        *args,
        scrub: bool = False,
        conditional_scrub: bool = False,
    ):
        async with self._lock:
            if self._connection is None:
                raise RuntimeError("SQLite job store is not started")

            def execute():
                result = operation(self._connection, *args)
                requested_scrub = scrub
                if conditional_scrub:
                    result, requested_scrub = result
                self._scrub_pending = (
                    self._scrub_pending or requested_scrub
                )
                if self._scrub_pending:
                    self._scrub_payload_history(self._connection)
                    self._scrub_pending = False
                return result

            operation_task = asyncio.create_task(
                asyncio.to_thread(execute)
            )
            try:
                return await asyncio.shield(operation_task)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await asyncio.shield(operation_task)
                raise

    @staticmethod
    def _scrub_payload_history(connection: sqlite3.Connection) -> None:
        checkpoint = connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if checkpoint is not None and checkpoint[0]:
            raise RuntimeError("SQLite payload scrub checkpoint was busy")

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA secure_delete=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS async_jobs (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                correlation_id TEXT,
                idempotency_key TEXT,
                status TEXT NOT NULL,
                payload BLOB,
                webhook_payload BLOB,
                webhook_event_status TEXT,
                webhook_attempt_count INTEGER NOT NULL DEFAULT 0,
                webhook_available_at REAL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL,
                queue_expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                started_at REAL,
                claim_token TEXT,
                request_fingerprint BLOB,
                completed_at REAL,
                artifact_key TEXT,
                media_type TEXT,
                filename TEXT,
                artifact_bytes INTEGER,
                result_expires_at REAL,
                queue_ms INTEGER,
                render_ms INTEGER,
                error_code TEXT,
                error_message TEXT,
                error_retryable INTEGER,
                CHECK (status IN (
                    'queued', 'running', 'succeeded', 'failed',
                    'cancelled', 'expired'
                ))
            );
            CREATE INDEX IF NOT EXISTS async_jobs_dispatch_idx
                ON async_jobs(status, available_at, created_at);
            CREATE INDEX IF NOT EXISTS async_jobs_cleanup_idx
                ON async_jobs(completed_at);
            """
        )
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(async_jobs)"
            ).fetchall()
        }
        if "claim_token" not in columns:
            connection.execute(
                "ALTER TABLE async_jobs ADD COLUMN claim_token TEXT"
            )
        if "request_fingerprint" not in columns:
            connection.execute(
                "ALTER TABLE async_jobs ADD COLUMN request_fingerprint BLOB"
            )
        if "correlation_id" not in columns:
            connection.execute(
                "ALTER TABLE async_jobs ADD COLUMN correlation_id TEXT"
            )
            connection.execute(
                "UPDATE async_jobs SET correlation_id=request_id"
            )
        if "idempotency_key" not in columns:
            connection.execute(
                "ALTER TABLE async_jobs ADD COLUMN idempotency_key TEXT"
            )
            connection.execute(
                "UPDATE async_jobs SET idempotency_key=request_id"
            )
        if "webhook_payload" not in columns:
            connection.execute(
                "ALTER TABLE async_jobs ADD COLUMN webhook_payload BLOB"
            )
        if "webhook_event_status" not in columns:
            connection.execute(
                "ALTER TABLE async_jobs ADD COLUMN webhook_event_status TEXT"
            )
        if "webhook_attempt_count" not in columns:
            connection.execute(
                "ALTER TABLE async_jobs ADD COLUMN webhook_attempt_count INTEGER NOT NULL DEFAULT 0"
            )
        if "webhook_available_at" not in columns:
            connection.execute(
                "ALTER TABLE async_jobs ADD COLUMN webhook_available_at REAL"
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS async_jobs_webhook_idx
            ON async_jobs(webhook_available_at, completed_at)
            WHERE webhook_payload IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS async_jobs_idempotency_idx
            ON async_jobs(idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> JobRecord | None:
        if row is None:
            return None
        return JobRecord(
            id=row["id"],
            request_id=row["correlation_id"] or row["request_id"],
            status=row["status"],
            payload=bytes(row["payload"]) if row["payload"] is not None else None,
            attempt_count=int(row["attempt_count"]),
            available_at=_datetime(row["available_at"]),
            queue_expires_at=_datetime(row["queue_expires_at"]),
            created_at=_datetime(row["created_at"]),
            idempotency_key=row["idempotency_key"],
            request_fingerprint=(
                bytes(row["request_fingerprint"])
                if row["request_fingerprint"] is not None
                else None
            ),
            webhook_payload=(
                bytes(row["webhook_payload"])
                if row["webhook_payload"] is not None
                else None
            ),
            webhook_event_status=row["webhook_event_status"],
            webhook_attempt_count=int(row["webhook_attempt_count"] or 0),
            webhook_available_at=_datetime(row["webhook_available_at"]),
            started_at=_datetime(row["started_at"]),
            completed_at=_datetime(row["completed_at"]),
            artifact_key=row["artifact_key"],
            media_type=row["media_type"],
            filename=row["filename"],
            artifact_bytes=row["artifact_bytes"],
            result_expires_at=_datetime(row["result_expires_at"]),
            queue_ms=row["queue_ms"],
            render_ms=row["render_ms"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            error_retryable=(
                bool(row["error_retryable"])
                if row["error_retryable"] is not None
                else None
            ),
        )

    async def create(self, job: JobRecord, active_limit: int) -> JobRecord:
        return await self._run(
            self._create,
            job,
            active_limit,
            conditional_scrub=True,
        )

    @classmethod
    def _create(
        cls,
        connection: sqlite3.Connection,
        job: JobRecord,
        active_limit: int,
    ) -> tuple[JobRecord, bool]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = _epoch(job.created_at)
            expired_queue = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired', payload = NULL,
                    webhook_event_status = CASE
                        WHEN webhook_payload IS NOT NULL THEN 'failed'
                        ELSE NULL END,
                    webhook_attempt_count = 0,
                    webhook_available_at = CASE
                        WHEN webhook_payload IS NOT NULL THEN ? ELSE NULL END,
                    completed_at = ?,
                    error_code = 'job_queue_expired',
                    error_message = 'The job expired before a worker could start it.',
                    error_retryable = 1
                WHERE status = 'queued' AND queue_expires_at <= ?
                """,
                (current, current, current),
            )
            connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired',
                    error_code = 'async_result_expired',
                    error_message = 'The async job result is no longer available.',
                    error_retryable = 0
                WHERE status = 'succeeded' AND result_expires_at <= ?
                """,
                (current,),
            )
            existing = None
            if job.idempotency_key is not None:
                existing = connection.execute(
                    "SELECT * FROM async_jobs WHERE idempotency_key = ?",
                    (job.idempotency_key,),
                ).fetchone()
            if existing:
                existing_fingerprint = existing["request_fingerprint"]
                if (
                    existing_fingerprint is None
                    or job.request_fingerprint is None
                    or not hmac.compare_digest(
                        existing_fingerprint,
                        job.request_fingerprint,
                    )
                ):
                    raise IdempotencyConflictError
                connection.execute("COMMIT")
                return cls._from_row(existing), bool(expired_queue.rowcount)
            active = connection.execute(
                """
                SELECT count(*) FROM async_jobs
                WHERE status IN ('queued', 'running')
                """
            ).fetchone()[0]
            if int(active) >= active_limit:
                raise QueueFullError
            connection.execute(
                """
                INSERT INTO async_jobs (
                    id, request_id, correlation_id, idempotency_key, status,
                    payload, webhook_payload, attempt_count,
                    available_at, queue_expires_at, created_at,
                    request_fingerprint
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.idempotency_key or job.id,
                    job.request_id,
                    job.idempotency_key,
                    job.payload,
                    job.webhook_payload,
                    _epoch(job.available_at),
                    _epoch(job.queue_expires_at),
                    _epoch(job.created_at),
                    job.request_fingerprint,
                ),
            )
            connection.execute("COMMIT")
            return job, bool(expired_queue.rowcount)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    async def claim(
        self,
        now: datetime,
        max_attempts: int,
        claim_token: str | None = None,
    ) -> JobRecord | None:
        return await self._run(
            self._claim,
            now,
            max_attempts,
            claim_token or str(uuid4()),
            conditional_scrub=True,
        )

    @classmethod
    def _claim(
        cls,
        connection: sqlite3.Connection,
        now: datetime,
        max_attempts: int,
        claim_token: str,
    ) -> tuple[JobRecord | None, bool]:
        current = _epoch(now)
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                """
                SELECT * FROM async_jobs
                WHERE status = 'running' AND claim_token = ?
                """,
                (claim_token,),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                return cls._from_row(existing), False
            exhausted_queue = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'failed', payload = NULL,
                    webhook_event_status = CASE
                        WHEN webhook_payload IS NOT NULL THEN 'failed'
                        ELSE NULL END,
                    webhook_attempt_count = 0,
                    webhook_available_at = CASE
                        WHEN webhook_payload IS NOT NULL THEN ? ELSE NULL END,
                    completed_at = ?,
                    error_code = 'job_attempts_exhausted',
                    error_message = 'The queued job exhausted its retry attempts.',
                    error_retryable = 0
                WHERE status = 'queued' AND attempt_count >= ?
                """,
                (current, current, max_attempts),
            )
            row = connection.execute(
                """
                SELECT * FROM async_jobs
                WHERE status = 'queued' AND available_at <= ?
                  AND queue_expires_at > ? AND attempt_count < ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (current, current, max_attempts),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None, bool(exhausted_queue.rowcount)
            started_at = row["started_at"] or current
            connection.execute(
                """
                UPDATE async_jobs
                SET status = 'running', attempt_count = attempt_count + 1,
                    started_at = ?, claim_token = ?
                WHERE id = ? AND status = 'queued'
                """,
                (started_at, claim_token, row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM async_jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()
            connection.execute("COMMIT")
            return cls._from_row(updated), bool(exhausted_queue.rowcount)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    async def get(self, job_id: str, now: datetime) -> JobRecord | None:
        return await self._run(
            self._get,
            job_id,
            now,
            conditional_scrub=True,
        )

    async def get_by_request_id(self, request_id: str) -> JobRecord | None:
        return await self._run(self._get_by_request_id, request_id)

    @classmethod
    def _get_by_request_id(
        cls,
        connection: sqlite3.Connection,
        request_id: str,
    ) -> JobRecord | None:
        return cls._from_row(
            connection.execute(
                "SELECT * FROM async_jobs "
                "WHERE coalesce(correlation_id, request_id) = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (request_id,),
            ).fetchone()
        )

    async def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> JobRecord | None:
        return await self._run(
            self._get_by_idempotency_key, idempotency_key
        )

    @classmethod
    def _get_by_idempotency_key(
        cls,
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> JobRecord | None:
        return cls._from_row(
            connection.execute(
                "SELECT * FROM async_jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        )

    @classmethod
    def _get(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
        now: datetime,
    ) -> tuple[JobRecord | None, bool]:
        current = _epoch(now)
        connection.execute("BEGIN IMMEDIATE")
        try:
            expired_queue = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired', payload = NULL,
                    webhook_event_status = CASE
                        WHEN webhook_payload IS NOT NULL THEN 'failed'
                        ELSE NULL END,
                    webhook_attempt_count = 0,
                    webhook_available_at = CASE
                        WHEN webhook_payload IS NOT NULL THEN ? ELSE NULL END,
                    completed_at = ?,
                    error_code = 'job_queue_expired',
                    error_message = 'The job expired before a worker could start it.',
                    error_retryable = 1
                WHERE id = ? AND status = 'queued' AND queue_expires_at <= ?
                """,
                (current, current, job_id, current),
            )
            connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired',
                    error_code = 'async_result_expired',
                    error_message = 'The async job result is no longer available.',
                    error_retryable = 0
                WHERE id = ? AND status = 'succeeded'
                  AND result_expires_at <= ?
                """,
                (job_id, current),
            )
            row = connection.execute(
                "SELECT * FROM async_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            connection.execute("COMMIT")
            return cls._from_row(row), bool(expired_queue.rowcount)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    async def cancel(self, job_id: str, now: datetime) -> JobRecord | None:
        return await self._run(
            self._cancel,
            job_id,
            now,
            conditional_scrub=True,
        )

    @classmethod
    def _cancel(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
        now: datetime,
    ) -> tuple[JobRecord | None, bool]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = _epoch(now)
            connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired',
                    error_code = 'async_result_expired',
                    error_message = 'The async job result is no longer available.',
                    error_retryable = 0
                WHERE id = ? AND status = 'succeeded'
                  AND result_expires_at <= ?
                """,
                (job_id, current),
            )
            row = connection.execute(
                "SELECT * FROM async_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None, False
            if row["status"] == "running":
                raise JobConflictError
            if row["status"] == "queued":
                if row["queue_expires_at"] <= current:
                    connection.execute(
                        """
                        UPDATE async_jobs
                        SET status = 'expired', payload = NULL,
                            webhook_event_status = CASE
                                WHEN webhook_payload IS NOT NULL THEN 'failed'
                                ELSE NULL END,
                            webhook_attempt_count = 0,
                            webhook_available_at = CASE
                                WHEN webhook_payload IS NOT NULL THEN ? ELSE NULL END,
                            completed_at = ?,
                            error_code = 'job_queue_expired',
                            error_message = 'The job expired before a worker could start it.',
                            error_retryable = 1
                        WHERE id = ?
                        """,
                        (current, current, job_id),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE async_jobs
                        SET status = 'cancelled', payload = NULL,
                            webhook_event_status = CASE
                                WHEN webhook_payload IS NOT NULL THEN 'cancelled'
                                ELSE NULL END,
                            webhook_attempt_count = 0,
                            webhook_available_at = CASE
                                WHEN webhook_payload IS NOT NULL THEN ? ELSE NULL END,
                            completed_at = ?,
                            error_code = 'job_cancelled',
                            error_message = 'The queued job was cancelled.',
                            error_retryable = 0
                        WHERE id = ?
                        """,
                        (current, current, job_id),
                    )
            updated = connection.execute(
                "SELECT * FROM async_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            connection.execute("COMMIT")
            return cls._from_row(updated), row["status"] == "queued"
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    async def succeed(
        self,
        job_id: str,
        *,
        expected_attempt: int,
        artifact_key: str,
        media_type: str,
        filename: str,
        artifact_bytes: int,
        result_expires_at: datetime,
        queue_ms: int,
    ) -> JobRecord:
        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[JobRecord, bool]:
            current = datetime.now(UTC).timestamp()
            existing = connection.execute(
                "SELECT * FROM async_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if (
                existing is not None
                and existing["status"] == "succeeded"
                and existing["attempt_count"] == expected_attempt
            ):
                expected = (
                    artifact_key,
                    media_type,
                    filename,
                    artifact_bytes,
                    _epoch(result_expires_at),
                    queue_ms,
                )
                actual = (
                    existing["artifact_key"],
                    existing["media_type"],
                    existing["filename"],
                    existing["artifact_bytes"],
                    existing["result_expires_at"],
                    existing["queue_ms"],
                )
                if actual != expected:
                    raise JobConflictError
                return self._from_row(existing), False
            if _epoch(result_expires_at) <= current:
                raise ArtifactExpiredError
            running = connection.execute(
                """
                SELECT started_at FROM async_jobs
                WHERE id = ? AND status = 'running' AND attempt_count = ?
                """,
                (job_id, expected_attempt),
            ).fetchone()
            render_ms = (
                max(0, round((current - running["started_at"]) * 1000))
                if running is not None and running["started_at"] is not None
                else 0
            )
            updated = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'succeeded', payload = NULL,
                    webhook_event_status = CASE
                        WHEN webhook_payload IS NOT NULL THEN 'succeeded'
                        ELSE NULL END,
                    webhook_attempt_count = 0,
                    webhook_available_at = CASE
                        WHEN webhook_payload IS NOT NULL THEN ? ELSE NULL END,
                    artifact_key = ?,
                    media_type = ?, filename = ?, artifact_bytes = ?,
                    result_expires_at = ?, queue_ms = ?, render_ms = ?,
                    completed_at = ?, error_code = NULL, error_message = NULL,
                    error_retryable = NULL
                WHERE id = ? AND status = 'running' AND attempt_count = ?
                """,
                (
                    current,
                    artifact_key,
                    media_type,
                    filename,
                    artifact_bytes,
                    _epoch(result_expires_at),
                    queue_ms,
                    render_ms,
                    current,
                    job_id,
                    expected_attempt,
                ),
            )
            if updated.rowcount != 1:
                existing = connection.execute(
                    "SELECT * FROM async_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                expected = (
                    artifact_key,
                    media_type,
                    filename,
                    artifact_bytes,
                    _epoch(result_expires_at),
                    queue_ms,
                )
                actual = (
                    (
                        existing["artifact_key"],
                        existing["media_type"],
                        existing["filename"],
                        existing["artifact_bytes"],
                        existing["result_expires_at"],
                        existing["queue_ms"],
                    )
                    if (
                        existing is not None
                        and existing["status"] == "succeeded"
                        and existing["attempt_count"] == expected_attempt
                    )
                    else None
                )
                if actual != expected:
                    raise JobConflictError
                return self._from_row(existing), False
            row = connection.execute(
                "SELECT * FROM async_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            return self._from_row(row), True

        return await self._run(operation, conditional_scrub=True)

    async def fail(
        self,
        job_id: str,
        *,
        expected_attempt: int,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[None, bool]:
            code_value = code[:64]
            message_value = message[:256]
            completed_at = datetime.now(UTC).timestamp()
            updated = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'failed', payload = NULL,
                    webhook_event_status = CASE
                        WHEN webhook_payload IS NOT NULL THEN 'failed'
                        ELSE NULL END,
                    webhook_attempt_count = 0,
                    webhook_available_at = CASE
                        WHEN webhook_payload IS NOT NULL THEN ? ELSE NULL END,
                    completed_at = ?,
                    error_code = ?, error_message = ?, error_retryable = ?
                WHERE id = ? AND status = 'running' AND attempt_count = ?
                """,
                (
                    completed_at,
                    completed_at,
                    code_value,
                    message_value,
                    retryable,
                    job_id,
                    expected_attempt,
                ),
            )
            if updated.rowcount == 1:
                return None, True
            existing = connection.execute(
                "SELECT * FROM async_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            expected = (
                code_value,
                message_value,
                bool(retryable),
            )
            actual = (
                (
                    existing["error_code"],
                    existing["error_message"],
                    bool(existing["error_retryable"]),
                )
                if (
                    existing is not None
                    and existing["status"] == "failed"
                    and existing["attempt_count"] == expected_attempt
                )
                else None
            )
            if actual != expected:
                raise JobConflictError
            return None, False

        await self._run(operation, conditional_scrub=True)

    async def requeue_running(
        self,
        now: datetime,
        max_attempts: int,
    ) -> int:
        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[int, bool]:
            current = _epoch(now)
            expired = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired', payload = NULL,
                    webhook_event_status = CASE
                        WHEN webhook_payload IS NOT NULL THEN 'failed'
                        ELSE NULL END,
                    webhook_attempt_count = 0,
                    webhook_available_at = CASE
                        WHEN webhook_payload IS NOT NULL THEN ? ELSE NULL END,
                    completed_at = ?,
                    error_code = 'job_queue_expired',
                    error_message = 'The job expired before a worker could restart it.',
                    error_retryable = 1
                WHERE status = 'running' AND queue_expires_at <= ?
                """,
                (current, current, current),
            ).rowcount
            exhausted = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'failed', payload = NULL,
                    webhook_event_status = CASE
                        WHEN webhook_payload IS NOT NULL THEN 'failed'
                        ELSE NULL END,
                    webhook_attempt_count = 0,
                    webhook_available_at = CASE
                        WHEN webhook_payload IS NOT NULL THEN ? ELSE NULL END,
                    completed_at = ?,
                    error_code = 'job_attempts_exhausted',
                    error_message = 'The interrupted job exhausted its retry attempts.',
                    error_retryable = 0
                WHERE status = 'running' AND queue_expires_at > ?
                  AND attempt_count >= ?
                """,
                (current, current, current, max_attempts),
            ).rowcount
            requeued = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'queued', available_at = ?, claim_token = NULL
                WHERE status = 'running' AND queue_expires_at > ?
                  AND attempt_count < ?
                """,
                (current, current, max_attempts),
            ).rowcount
            return (
                int(expired) + int(exhausted) + int(requeued),
                bool(expired or exhausted),
            )

        return await self._run(operation, conditional_scrub=True)

    async def requeue(
        self,
        job_id: str,
        expected_attempt: int,
        available_at: datetime,
    ) -> None:
        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[None, bool]:
            current = _epoch(available_at)
            updated = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'queued', available_at = ?
                WHERE id = ? AND status = 'running'
                  AND attempt_count = ? AND queue_expires_at > ?
                """,
                (current, job_id, expected_attempt, current),
            )
            expired = 0
            if updated.rowcount == 0:
                completed_at = datetime.now(UTC).timestamp()
                expired = connection.execute(
                    """
                    UPDATE async_jobs
                    SET status = 'expired', payload = NULL,
                        webhook_event_status = CASE
                            WHEN webhook_payload IS NOT NULL THEN 'failed'
                            ELSE NULL END,
                        webhook_attempt_count = 0,
                        webhook_available_at = CASE
                            WHEN webhook_payload IS NOT NULL THEN ? ELSE NULL END,
                        completed_at = ?,
                        error_code = 'job_queue_expired',
                        error_message = 'The interrupted job expired.',
                        error_retryable = 1
                    WHERE id = ? AND status = 'running'
                      AND attempt_count = ? AND queue_expires_at <= ?
                    """,
                    (
                        completed_at,
                        completed_at,
                        job_id,
                        expected_attempt,
                        current,
                    ),
                ).rowcount
            return None, bool(expired)

        await self._run(operation, conditional_scrub=True)

    async def defer(
        self,
        job_id: str,
        expected_attempt: int,
        available_at: datetime,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            updated = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'queued', available_at = ?,
                    attempt_count = attempt_count - 1, claim_token = NULL,
                    started_at = NULL
                WHERE id = ? AND status = 'running' AND attempt_count = ?
                """,
                (_epoch(available_at), job_id, expected_attempt),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise JobConflictError
            connection.commit()

        await self._run(operation)

    async def maintain(self, now: datetime) -> list[str]:
        return await self._run(
            self._maintain,
            now,
            self.config.metadata_ttl.total_seconds(),
            conditional_scrub=True,
        )

    @classmethod
    def _maintain(
        cls,
        connection: sqlite3.Connection,
        now: datetime,
        metadata_ttl: float,
    ) -> tuple[list[str], bool]:
        current = _epoch(now)
        cutoff = current - metadata_ttl
        connection.execute("BEGIN IMMEDIATE")
        try:
            keys = [
                row[0]
                for row in connection.execute(
                    """
                    WITH candidates(artifact_key) AS (
                        SELECT artifact_key FROM async_jobs
                        WHERE status = 'succeeded' AND result_expires_at <= ?
                          AND artifact_key IS NOT NULL
                        UNION
                        SELECT artifact_key FROM async_jobs
                        WHERE status = 'expired'
                          AND error_code = 'async_result_expired'
                          AND artifact_key IS NOT NULL
                        UNION
                        SELECT artifact_key FROM async_jobs
                        WHERE status IN ('failed', 'cancelled', 'expired')
                          AND completed_at < ? AND artifact_key IS NOT NULL
                    )
                    SELECT artifact_key FROM candidates AS candidate
                    WHERE NOT EXISTS (
                        SELECT 1 FROM async_jobs AS live
                        WHERE live.artifact_key = candidate.artifact_key
                          AND live.status = 'succeeded'
                          AND live.result_expires_at > ?
                    )
                    """,
                    (current, cutoff, current),
                ).fetchall()
            ]
            expired_queue = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired', payload = NULL,
                    webhook_event_status = CASE
                        WHEN webhook_payload IS NOT NULL THEN 'failed'
                        ELSE NULL END,
                    webhook_attempt_count = 0,
                    webhook_available_at = CASE
                        WHEN webhook_payload IS NOT NULL THEN ? ELSE NULL END,
                    completed_at = ?,
                    error_code = 'job_queue_expired',
                    error_message = 'The job expired before a worker could start it.',
                    error_retryable = 1
                WHERE status = 'queued' AND queue_expires_at <= ?
                """,
                (current, current, current),
            )
            connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired',
                    error_code = 'async_result_expired',
                    error_message = 'The async job result is no longer available.',
                    error_retryable = 0
                WHERE status = 'succeeded' AND result_expires_at <= ?
                """,
                (current,),
            )
            connection.execute(
                """
                DELETE FROM async_jobs
                WHERE status IN ('failed', 'cancelled', 'expired')
                  AND completed_at < ?
                  AND artifact_key IS NULL
                  AND webhook_payload IS NULL
                """,
                (cutoff,),
            )
            connection.execute("COMMIT")
            return (
                list(dict.fromkeys(keys)),
                bool(expired_queue.rowcount),
            )
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    async def pending_notifications(
        self, now: datetime, limit: int = 100
    ) -> list[JobRecord]:
        return await self._run(
            self._pending_notifications,
            now,
            max(1, min(limit, 100)),
        )

    @classmethod
    def _pending_notifications(
        cls,
        connection: sqlite3.Connection,
        now: datetime,
        limit: int,
    ) -> list[JobRecord]:
        rows = connection.execute(
            """
            SELECT * FROM async_jobs
            WHERE webhook_payload IS NOT NULL
              AND webhook_event_status IN ('succeeded', 'failed', 'cancelled')
              AND COALESCE(webhook_available_at, completed_at, 0) <= ?
            ORDER BY COALESCE(webhook_available_at, completed_at, 0), id
            LIMIT ?
            """,
            (_epoch(now), limit),
        ).fetchall()
        return [cls._from_row(row) for row in rows]

    async def defer_notification(
        self,
        job_id: str,
        webhook_payload: bytes,
        available_at: datetime,
    ) -> None:
        await self._run(
            lambda connection: connection.execute(
                """
                UPDATE async_jobs
                SET webhook_attempt_count = webhook_attempt_count + 1,
                    webhook_available_at = ?
                WHERE id = ? AND webhook_payload = ?
                """,
                (_epoch(available_at), job_id, webhook_payload),
            )
        )

    async def acknowledge_notification(
        self, job_id: str, webhook_payload: bytes
    ) -> None:
        def operation(connection: sqlite3.Connection) -> tuple[None, bool]:
            updated = connection.execute(
                """
                UPDATE async_jobs
                SET webhook_payload = NULL, webhook_event_status = NULL,
                    webhook_attempt_count = 0, webhook_available_at = NULL
                WHERE id = ? AND webhook_payload = ?
                """,
                (job_id, webhook_payload),
            )
            return None, bool(updated.rowcount)

        await self._run(operation, conditional_scrub=True)

    async def acknowledge_artifact_deletion(self, key: str) -> None:
        await self._run(
            lambda connection: connection.execute(
                """
                UPDATE async_jobs
                SET artifact_key = NULL
                WHERE artifact_key = ?
                  AND status IN ('failed', 'cancelled', 'expired')
                """,
                (key,),
            )
        )

    async def expire_result(
        self,
        job_id: str,
        artifact_key: str,
        now: datetime,
    ) -> JobRecord | None:
        return await self._run(
            self._expire_result,
            job_id,
            artifact_key,
            now,
        )

    @classmethod
    def _expire_result(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
        artifact_key: str,
        now: datetime,
    ) -> JobRecord | None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired',
                    error_code = 'async_result_expired',
                    error_message = 'The async job result is no longer available.',
                    error_retryable = 0
                WHERE id = ? AND status = 'succeeded' AND artifact_key = ?
                """,
                (job_id, artifact_key),
            )
            row = connection.execute(
                "SELECT * FROM async_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            connection.execute("COMMIT")
            return cls._from_row(row)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


_SAFE_KEY = re.compile(
    r"^[0-9a-f-]{36}\.(png|jpg|webp|avif|gif|pdf|html|md|json|zip|webm|mp4)$"
)
_ARTIFACT_EXTENSIONS = {
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


class LocalArtifactStore:
    """Atomic expiring files with JSON metadata stored outside the web root."""

    def __init__(self, config: ArtifactStoreConfig) -> None:
        self.config = config
        self.root = config.data_dir / "job-results"
        self._publishing: set[str] = set()
        self._publishing_lock = threading.Lock()

    async def start(self) -> None:
        if os.name == "nt":
            raise RuntimeError(
                "The bundled local artifact store requires POSIX owner-only "
                "permissions; configure an external artifact store on Windows."
            )
        await asyncio.to_thread(
            _ensure_private_directory,
            self.config.data_dir,
        )
        await asyncio.to_thread(_ensure_private_directory, self.root)

    async def close(self) -> None:
        return None

    def _paths(self, key: str) -> tuple[Path, Path]:
        if not _SAFE_KEY.fullmatch(key):
            raise ValueError("invalid local artifact key")
        return self.root / key, self.root / f"{key}.json"

    async def put(
        self,
        job_id: str,
        body: bytes,
        *,
        media_type: str,
        filename: str,
    ) -> StoredArtifact:
        extension = _ARTIFACT_EXTENSIONS.get(media_type)
        if extension is None:
            raise ValueError("unsupported async artifact media type")
        key = f"{uuid4()}.{extension}"
        expires_at = await asyncio.to_thread(
            self._put,
            key,
            body,
            media_type,
            filename,
        )
        return StoredArtifact(key, expires_at)

    def _put(
        self,
        key: str,
        body: bytes,
        media_type: str,
        filename: str,
    ) -> datetime:
        data_path, metadata_path = self._paths(key)
        token = uuid4().hex
        data_temp = self.root / f".{key}.{token}.tmp"
        metadata_temp = self.root / f".{key}.{token}.json.tmp"
        with self._publishing_lock:
            self._publishing.add(key)
        try:
            self._write_durable(data_temp, body)
            os.replace(data_temp, data_path)
            self._sync_directory()
            metadata = json.dumps(
                {
                    "media_type": media_type,
                    "filename": filename,
                    "ttl_seconds": self.config.result_ttl.total_seconds(),
                },
                separators=(",", ":"),
            ).encode("utf-8")
            self._write_durable(
                metadata_temp,
                metadata,
            )
            os.replace(metadata_temp, metadata_path)
            self._sync_directory()
            completed_at = datetime.now(UTC)
            timestamp = completed_at.timestamp()
            os.utime(metadata_path, (timestamp, timestamp))
            descriptor = os.open(metadata_path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._sync_directory()
            expires_at = completed_at + self.config.result_ttl
            return expires_at
        finally:
            with self._publishing_lock:
                self._publishing.discard(key)
            for path in (data_temp, metadata_temp):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _write_durable(path: Path, body: bytes) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            view = memoryview(body)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _sync_directory(self) -> None:
        _sync_directory(self.root)

    def _sync_parent_directory(self) -> None:
        _sync_directory(self.root.parent)

    @staticmethod
    def _read_private_file(path: Path) -> tuple[bytes, float]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RuntimeError(
                    "async artifact must be a private regular file"
                ) from exc
            raise
        try:
            information = os.fstat(descriptor)
            if (
                not stat.S_ISREG(information.st_mode)
                or information.st_uid != os.getuid()
                or information.st_mode & 0o077
            ):
                raise RuntimeError(
                    "async artifact must be an owner-only regular file"
                )
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 65536):
                chunks.append(chunk)
            return b"".join(chunks), information.st_mtime
        finally:
            os.close(descriptor)

    async def get(self, key: str) -> Artifact | None:
        operation = asyncio.create_task(asyncio.to_thread(self._get, key))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(operation)
            raise

    def _get(self, key: str) -> Artifact | None:
        data_path, metadata_path = self._paths(key)
        try:
            metadata_body, metadata_mtime = self._read_private_file(
                metadata_path
            )
            metadata = json.loads(metadata_body)
            if not isinstance(metadata, dict):
                raise ValueError("invalid async artifact metadata")
            expires_at = self._metadata_expiry(metadata_mtime, metadata)
            if expires_at <= datetime.now(UTC):
                self._delete(key)
                return None
            body, _data_mtime = self._read_private_file(data_path)
            return Artifact(
                key=key,
                body=body,
                media_type=str(metadata["media_type"]),
                filename=str(metadata["filename"]),
            )
        except FileNotFoundError:
            return None
        except (
            RuntimeError,
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            return None

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete, key)

    def _delete(self, key: str) -> None:
        removed = False
        for path in self._paths(key):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
        if removed:
            self._sync_directory()

    async def maintain(self, now: datetime) -> None:
        await asyncio.to_thread(self._maintain, now)

    def _maintain(self, now: datetime) -> None:
        oldest_temporary = now.timestamp() - max(
            self.config.result_ttl.total_seconds(),
            3600,
        )
        removed_temporary = False
        for temporary in self.root.glob(".*.tmp"):
            try:
                abandoned = temporary.stat().st_mtime <= oldest_temporary
            except OSError:
                continue
            if abandoned:
                try:
                    temporary.unlink()
                    removed_temporary = True
                except FileNotFoundError:
                    pass
        if removed_temporary:
            self._sync_directory()
        for metadata_path in self.root.glob("*.json"):
            key = metadata_path.name[:-5]
            if not _SAFE_KEY.fullmatch(key):
                continue
            try:
                metadata_body, metadata_mtime = self._read_private_file(
                    metadata_path
                )
                metadata = json.loads(metadata_body)
                if not isinstance(metadata, dict):
                    raise ValueError("invalid async artifact metadata")
                expires_at = self._metadata_expiry(
                    metadata_mtime,
                    metadata,
                )
            except FileNotFoundError:
                continue
            except OSError:
                continue
            except (
                RuntimeError,
                KeyError,
                TypeError,
                ValueError,
                OverflowError,
            ):
                self._delete(key)
                continue
            if expires_at <= now:
                self._delete(key)
        oldest_orphan = now.timestamp() - self.config.result_ttl.total_seconds()
        for data_path in self.root.iterdir():
            if not data_path.is_file() or not _SAFE_KEY.fullmatch(data_path.name):
                continue
            metadata_path = self.root / f"{data_path.name}.json"
            try:
                with self._publishing_lock:
                    publishing = data_path.name in self._publishing
                orphaned = (
                    not publishing and not metadata_path.exists()
                    and data_path.stat().st_mtime <= oldest_orphan
                )
            except OSError:
                continue
            if orphaned:
                self._delete(data_path.name)

    @staticmethod
    def _metadata_expiry(
        metadata_mtime: float,
        metadata: dict[str, object],
    ) -> datetime:
        if "ttl_seconds" in metadata:
            return datetime.fromtimestamp(
                metadata_mtime,
                UTC,
            ) + timedelta(seconds=float(metadata["ttl_seconds"]))
        return datetime.fromisoformat(str(metadata["expires_at"]))


def create_sqlite_job_store(config: JobStoreConfig) -> SQLiteJobStore:
    """Factory usable as VIPERCAPTURE_JOB_STORE_FACTORY."""

    return SQLiteJobStore(config)


def create_local_artifact_store(
    config: ArtifactStoreConfig,
) -> LocalArtifactStore:
    """Factory usable as VIPERCAPTURE_ARTIFACT_STORE_FACTORY."""

    return LocalArtifactStore(config)
