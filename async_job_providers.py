"""Bundled SQLite job state and filesystem artifact providers."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from uuid import uuid4

from async_jobs import (
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
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        await self._run(self._initialize)
        for path in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            with suppress(OSError):
                path.chmod(0o600)

    async def close(self) -> None:
        if self._connection is None:
            return
        async with self._lock:
            connection, self._connection = self._connection, None
            await asyncio.to_thread(connection.close)

    async def _run(self, operation, *args, scrub: bool = False):
        async with self._lock:
            if self._connection is None:
                raise RuntimeError("SQLite job store is not started")

            def execute():
                result = operation(self._connection, *args)
                if scrub:
                    self._scrub_payload_history(self._connection)
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
                status TEXT NOT NULL,
                payload BLOB,
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

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> JobRecord | None:
        if row is None:
            return None
        return JobRecord(
            id=row["id"],
            request_id=row["request_id"],
            status=row["status"],
            payload=bytes(row["payload"]) if row["payload"] is not None else None,
            attempt_count=int(row["attempt_count"]),
            available_at=_datetime(row["available_at"]),
            queue_expires_at=_datetime(row["queue_expires_at"]),
            created_at=_datetime(row["created_at"]),
            request_fingerprint=(
                bytes(row["request_fingerprint"])
                if row["request_fingerprint"] is not None
                else None
            ),
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
        return await self._run(self._create, job, active_limit, scrub=True)

    @classmethod
    def _create(
        cls,
        connection: sqlite3.Connection,
        job: JobRecord,
        active_limit: int,
    ) -> JobRecord:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = _epoch(job.created_at)
            connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired', payload = NULL, completed_at = ?,
                    error_code = 'job_queue_expired',
                    error_message = 'The job expired before a worker could start it.',
                    error_retryable = 1
                WHERE status = 'queued' AND queue_expires_at <= ?
                """,
                (current, current),
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
            existing = connection.execute(
                "SELECT * FROM async_jobs WHERE request_id = ?",
                (job.request_id,),
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
                return cls._from_row(existing)
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
                    id, request_id, status, payload, attempt_count,
                    available_at, queue_expires_at, created_at,
                    request_fingerprint
                ) VALUES (?, ?, 'queued', ?, 0, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.request_id,
                    job.payload,
                    _epoch(job.available_at),
                    _epoch(job.queue_expires_at),
                    _epoch(job.created_at),
                    job.request_fingerprint,
                ),
            )
            connection.execute("COMMIT")
            return job
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
            scrub=True,
        )

    @classmethod
    def _claim(
        cls,
        connection: sqlite3.Connection,
        now: datetime,
        max_attempts: int,
        claim_token: str,
    ) -> JobRecord | None:
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
                return cls._from_row(existing)
            connection.execute(
                """
                UPDATE async_jobs
                SET status = 'failed', payload = NULL, completed_at = ?,
                    error_code = 'job_attempts_exhausted',
                    error_message = 'The queued job exhausted its retry attempts.',
                    error_retryable = 0
                WHERE status = 'queued' AND attempt_count >= ?
                """,
                (current, max_attempts),
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
                return None
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
            return cls._from_row(updated)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    async def get(self, job_id: str, now: datetime) -> JobRecord | None:
        return await self._run(self._get, job_id, now, scrub=True)

    @classmethod
    def _get(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
        now: datetime,
    ) -> JobRecord | None:
        current = _epoch(now)
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired', payload = NULL, completed_at = ?,
                    error_code = 'job_queue_expired',
                    error_message = 'The job expired before a worker could start it.',
                    error_retryable = 1
                WHERE id = ? AND status = 'queued' AND queue_expires_at <= ?
                """,
                (current, job_id, current),
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
            return cls._from_row(row)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    async def cancel(self, job_id: str, now: datetime) -> JobRecord | None:
        return await self._run(self._cancel, job_id, now, scrub=True)

    @classmethod
    def _cancel(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
        now: datetime,
    ) -> JobRecord | None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM async_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            if row["status"] == "running":
                raise JobConflictError
            if row["status"] == "queued":
                current = _epoch(now)
                if row["queue_expires_at"] <= current:
                    connection.execute(
                        """
                        UPDATE async_jobs
                        SET status = 'expired', payload = NULL,
                            completed_at = ?,
                            error_code = 'job_queue_expired',
                            error_message = 'The job expired before a worker could start it.',
                            error_retryable = 1
                        WHERE id = ?
                        """,
                        (current, job_id),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE async_jobs
                        SET status = 'cancelled', payload = NULL,
                            completed_at = ?,
                            error_code = 'job_cancelled',
                            error_message = 'The queued job was cancelled.',
                            error_retryable = 0
                        WHERE id = ?
                        """,
                        (current, job_id),
                    )
            updated = connection.execute(
                "SELECT * FROM async_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            connection.execute("COMMIT")
            return cls._from_row(updated)
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
        def operation(connection: sqlite3.Connection) -> JobRecord:
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
                return self._from_row(existing)
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
                SET status = 'succeeded', payload = NULL, artifact_key = ?,
                    media_type = ?, filename = ?, artifact_bytes = ?,
                    result_expires_at = ?, queue_ms = ?, render_ms = ?,
                    completed_at = ?, error_code = NULL, error_message = NULL,
                    error_retryable = NULL
                WHERE id = ? AND status = 'running' AND attempt_count = ?
                """,
                (
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
                return self._from_row(existing)
            row = connection.execute(
                "SELECT * FROM async_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            return self._from_row(row)

        return await self._run(operation, scrub=True)

    async def fail(
        self,
        job_id: str,
        *,
        expected_attempt: int,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            code_value = code[:64]
            message_value = message[:256]
            completed_at = datetime.now(UTC).timestamp()
            updated = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'failed', payload = NULL, completed_at = ?,
                    error_code = ?, error_message = ?, error_retryable = ?
                WHERE id = ? AND status = 'running' AND attempt_count = ?
                """,
                (
                    completed_at,
                    code_value,
                    message_value,
                    retryable,
                    job_id,
                    expected_attempt,
                ),
            )
            if updated.rowcount == 1:
                return
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

        await self._run(operation, scrub=True)

    async def requeue_running(
        self,
        now: datetime,
        max_attempts: int,
    ) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            current = _epoch(now)
            expired = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired', payload = NULL, completed_at = ?,
                    error_code = 'job_queue_expired',
                    error_message = 'The job expired before a worker could restart it.',
                    error_retryable = 1
                WHERE status = 'running' AND queue_expires_at <= ?
                """,
                (current, current),
            ).rowcount
            exhausted = connection.execute(
                """
                UPDATE async_jobs
                SET status = 'failed', payload = NULL, completed_at = ?,
                    error_code = 'job_attempts_exhausted',
                    error_message = 'The interrupted job exhausted its retry attempts.',
                    error_retryable = 0
                WHERE status = 'running' AND queue_expires_at > ?
                  AND attempt_count >= ?
                """,
                (current, current, max_attempts),
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
            return int(expired) + int(exhausted) + int(requeued)

        return await self._run(operation, scrub=True)

    async def requeue(
        self,
        job_id: str,
        expected_attempt: int,
        available_at: datetime,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
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
            if updated.rowcount == 0:
                completed_at = datetime.now(UTC).timestamp()
                connection.execute(
                    """
                    UPDATE async_jobs
                    SET status = 'expired', payload = NULL, completed_at = ?,
                        error_code = 'job_queue_expired',
                        error_message = 'The interrupted job expired.',
                        error_retryable = 1
                    WHERE id = ? AND status = 'running'
                      AND attempt_count = ? AND queue_expires_at <= ?
                    """,
                    (completed_at, job_id, expected_attempt, current),
                )

        await self._run(operation, scrub=True)

    async def maintain(self, now: datetime) -> list[str]:
        return await self._run(
            self._maintain,
            now,
            self.config.metadata_ttl.total_seconds(),
            scrub=True,
        )

    @staticmethod
    def _maintain(
        connection: sqlite3.Connection,
        now: datetime,
        metadata_ttl: float,
    ) -> list[str]:
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
            connection.execute(
                """
                UPDATE async_jobs
                SET status = 'expired', payload = NULL, completed_at = ?,
                    error_code = 'job_queue_expired',
                    error_message = 'The job expired before a worker could start it.',
                    error_retryable = 1
                WHERE status = 'queued' AND queue_expires_at <= ?
                """,
                (current, current),
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
                """,
                (cutoff,),
            )
            connection.execute("COMMIT")
            return list(dict.fromkeys(keys))
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

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


_SAFE_KEY = re.compile(r"^[0-9a-f-]{36}\.(png|jpg|webp)$")


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
        await asyncio.to_thread(_ensure_private_directory, self.root)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

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
        extension = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
        }.get(media_type)
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

    async def get(self, key: str) -> Artifact | None:
        return await asyncio.to_thread(self._get, key)

    def _get(self, key: str) -> Artifact | None:
        data_path, metadata_path = self._paths(key)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expires_at = self._metadata_expiry(metadata_path, metadata)
            if expires_at <= datetime.now(UTC):
                self._delete(key)
                return None
            return Artifact(
                key=key,
                body=data_path.read_bytes(),
                media_type=str(metadata["media_type"]),
                filename=str(metadata["filename"]),
            )
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
            return None

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete, key)

    def _delete(self, key: str) -> None:
        for path in self._paths(key):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    async def maintain(self, now: datetime) -> None:
        await asyncio.to_thread(self._maintain, now)

    def _maintain(self, now: datetime) -> None:
        oldest_temporary = now.timestamp() - max(
            self.config.result_ttl.total_seconds(),
            3600,
        )
        for temporary in self.root.glob(".*.tmp"):
            try:
                abandoned = temporary.stat().st_mtime <= oldest_temporary
            except OSError:
                continue
            if abandoned:
                with suppress(FileNotFoundError):
                    temporary.unlink()
        for metadata_path in self.root.glob("*.json"):
            key = metadata_path.name[:-5]
            if not _SAFE_KEY.fullmatch(key):
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                expires_at = self._metadata_expiry(metadata_path, metadata)
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
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
                try:
                    data_path.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _metadata_expiry(
        metadata_path: Path,
        metadata: dict[str, object],
    ) -> datetime:
        if "ttl_seconds" in metadata:
            return datetime.fromtimestamp(
                metadata_path.stat().st_mtime,
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
