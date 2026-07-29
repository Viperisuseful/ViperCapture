"""Provider-neutral durable async render jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from render_contract import RenderRequest
from render_errors import RenderError


UTC = timezone.utc
PAYLOAD_VERSION = b"vipercapture-open-async-v1\0"
logger = logging.getLogger("vipercapture.async_jobs")


@dataclass(frozen=True)
class JobSettings:
    data_dir: Path
    queue_limit: int = 30
    worker_count: int = 1
    queue_ttl: timedelta = timedelta(minutes=15)
    result_ttl: timedelta = timedelta(hours=1)
    metadata_ttl: timedelta = timedelta(hours=24)
    max_attempts: int = 3
    poll_seconds: float = 5.0


@dataclass(frozen=True)
class JobStoreConfig:
    data_dir: Path
    metadata_ttl: timedelta


@dataclass(frozen=True)
class ArtifactStoreConfig:
    data_dir: Path
    result_ttl: timedelta


@dataclass(frozen=True)
class JobRecord:
    id: str
    request_id: str
    status: str
    payload: bytes | None
    attempt_count: int
    available_at: datetime
    queue_expires_at: datetime
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    artifact_key: str | None = None
    media_type: str | None = None
    filename: str | None = None
    artifact_bytes: int | None = None
    result_expires_at: datetime | None = None
    queue_ms: int | None = None
    render_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_retryable: bool | None = None


@dataclass(frozen=True)
class Artifact:
    key: str
    body: bytes
    media_type: str
    filename: str


@dataclass(frozen=True)
class RenderedArtifact:
    body: bytes
    media_type: str
    filename: str
    render_ms: int


@dataclass(frozen=True)
class StoredArtifact:
    key: str
    expires_at: datetime


class QueueFullError(Exception):
    pass


class JobConflictError(Exception):
    pass


@runtime_checkable
class JobStore(Protocol):
    """Atomic durable state operations required from a database adapter."""

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def create(self, job: JobRecord, active_limit: int) -> JobRecord: ...
    async def claim(
        self,
        now: datetime,
        max_attempts: int,
    ) -> JobRecord | None: ...
    async def get(self, job_id: str, now: datetime) -> JobRecord | None: ...
    async def cancel(self, job_id: str, now: datetime) -> JobRecord | None: ...
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
        render_ms: int,
        now: datetime,
    ) -> JobRecord: ...
    async def fail(
        self,
        job_id: str,
        *,
        expected_attempt: int,
        code: str,
        message: str,
        retryable: bool,
        now: datetime,
    ) -> None: ...
    async def requeue_running(
        self,
        now: datetime,
        max_attempts: int,
    ) -> int: ...
    async def requeue(
        self,
        job_id: str,
        expected_attempt: int,
        available_at: datetime,
    ) -> None: ...
    async def maintain(self, now: datetime) -> list[str]: ...
    async def acknowledge_artifact_deletion(self, key: str) -> None: ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Expiring binary storage required from a storage adapter."""

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def put(
        self,
        job_id: str,
        body: bytes,
        *,
        media_type: str,
        filename: str,
    ) -> StoredArtifact: ...
    async def get(self, key: str) -> Artifact | None: ...
    async def delete(self, key: str) -> None: ...
    async def maintain(self, now: datetime) -> None: ...


RenderJob = Callable[[RenderRequest], Awaitable[RenderedArtifact]]


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def settings_from_environment(
    *,
    default_workers: int = 1,
    base_dir: Path | None = None,
) -> JobSettings:
    root = Path(
        os.getenv(
            "VIPERCAPTURE_DATA_DIR",
            str((base_dir or Path.home()) / ".vipercapture"),
        )
    ).expanduser()
    return JobSettings(
        data_dir=root,
        queue_limit=_positive_int("VIPERCAPTURE_JOB_QUEUE_LIMIT", 30),
        worker_count=_positive_int(
            "VIPERCAPTURE_JOB_WORKERS",
            default_workers,
        ),
        queue_ttl=timedelta(
            seconds=_positive_int("VIPERCAPTURE_JOB_QUEUE_TTL_SECONDS", 900)
        ),
        result_ttl=timedelta(
            seconds=_positive_int("VIPERCAPTURE_JOB_RESULT_TTL_SECONDS", 3600)
        ),
        metadata_ttl=timedelta(
            seconds=_positive_int("VIPERCAPTURE_JOB_METADATA_TTL_SECONDS", 86400)
        ),
        max_attempts=_positive_int("VIPERCAPTURE_JOB_MAX_ATTEMPTS", 3),
    )


def _factory(spec: str, config: object) -> object:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("provider factories must use module:function syntax")
    factory = getattr(importlib.import_module(module_name), attribute)
    provider = factory(config)
    if inspect.isawaitable(provider):
        raise TypeError("provider factories must return an instance synchronously")
    return provider


def load_providers(settings: JobSettings) -> tuple[JobStore, ArtifactStore]:
    from async_job_providers import LocalArtifactStore, SQLiteJobStore

    job_spec = os.getenv("VIPERCAPTURE_JOB_STORE_FACTORY")
    artifact_spec = os.getenv("VIPERCAPTURE_ARTIFACT_STORE_FACTORY")
    job_config = JobStoreConfig(settings.data_dir, settings.metadata_ttl)
    artifact_config = ArtifactStoreConfig(settings.data_dir, settings.result_ttl)
    job_store = (
        _factory(job_spec, job_config)
        if job_spec
        else SQLiteJobStore(job_config)
    )
    artifact_store = (
        _factory(artifact_spec, artifact_config)
        if artifact_spec
        else LocalArtifactStore(artifact_config)
    )
    if not isinstance(job_store, JobStore):
        raise TypeError("job store does not implement the JobStore protocol")
    if not isinstance(artifact_store, ArtifactStore):
        raise TypeError("artifact store does not implement the ArtifactStore protocol")
    return job_store, artifact_store


def _read_or_create_key(data_dir: Path) -> bytes:
    supplied = os.getenv("VIPERCAPTURE_JOB_SECRET")
    if supplied:
        return sha256(PAYLOAD_VERSION + supplied.encode("utf-8")).digest()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "async-jobs.key"
    try:
        material = path.read_bytes()
    except FileNotFoundError:
        material = os.urandom(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            material = path.read_bytes()
        else:
            try:
                os.write(descriptor, material)
            finally:
                os.close(descriptor)
    if len(material) < 32:
        raise RuntimeError("async job key must contain at least 32 bytes")
    with suppress(OSError):
        path.chmod(0o600)
    return sha256(PAYLOAD_VERSION + material).digest()


class PayloadCipher:
    def __init__(self, key: bytes) -> None:
        self._cipher = AESGCM(key)

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "PayloadCipher":
        return cls(_read_or_create_key(data_dir))

    def encrypt(self, job_id: str, payload: RenderRequest) -> bytes:
        nonce = os.urandom(12)
        plaintext = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return nonce + self._cipher.encrypt(
            nonce,
            plaintext,
            job_id.encode("ascii"),
        )

    def decrypt(self, job: JobRecord) -> RenderRequest:
        if job.payload is None or len(job.payload) <= 28:
            raise RenderError(
                "job_payload_unavailable",
                "The queued render input is no longer available.",
                409,
                False,
            )
        nonce, ciphertext = job.payload[:12], job.payload[12:]
        try:
            plaintext = self._cipher.decrypt(
                nonce,
                ciphertext,
                job.id.encode("ascii"),
            )
            return RenderRequest.model_validate_json(plaintext)
        except Exception as exc:
            raise RenderError(
                "job_payload_invalid",
                "The queued render input could not be authenticated.",
                409,
                False,
            ) from exc


class AsyncJobService:
    def __init__(
        self,
        settings: JobSettings,
        job_store: JobStore,
        artifact_store: ArtifactStore,
        renderer: RenderJob,
        *,
        cipher: PayloadCipher | None = None,
    ) -> None:
        self.settings = settings
        self.job_store = job_store
        self.artifact_store = artifact_store
        self.renderer = renderer
        self.cipher = cipher or PayloadCipher.for_data_dir(settings.data_dir)
        self._wakeup = asyncio.Event()
        self._workers: list[asyncio.Task] = []
        self._closing = False
        self._maintenance_lock = asyncio.Lock()
        self._next_maintenance_at = 0.0
        self._maintenance_interval = max(1.0, settings.poll_seconds)

    async def start(self) -> None:
        self._closing = False
        await self.job_store.start()
        try:
            await self.artifact_store.start()
            await self.job_store.requeue_running(
                datetime.now(UTC),
                self.settings.max_attempts,
            )
            await self._maintain(force=True)
            self._workers = [
                asyncio.create_task(
                    self._worker(index),
                    name=f"vipercapture-job-worker-{index + 1}",
                )
                for index in range(self.settings.worker_count)
            ]
        except BaseException:
            with suppress(Exception):
                await self.artifact_store.close()
            await self.job_store.close()
            raise

    async def close(self) -> None:
        self._closing = True
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        try:
            await self.artifact_store.close()
        finally:
            await self.job_store.close()

    async def submit(
        self,
        payload: RenderRequest,
        *,
        request_id: str,
    ) -> JobRecord:
        await self._maintain()
        now = datetime.now(UTC)
        job_id = str(uuid4())
        job = JobRecord(
            id=job_id,
            request_id=request_id,
            status="queued",
            payload=self.cipher.encrypt(job_id, payload),
            attempt_count=0,
            available_at=now,
            queue_expires_at=now + self.settings.queue_ttl,
            created_at=now,
        )
        try:
            stored = await self.job_store.create(job, self.settings.queue_limit)
        except QueueFullError as exc:
            raise RenderError(
                "async_queue_full",
                "The async render queue is full.",
                503,
                True,
                {"limit": self.settings.queue_limit},
                {"Retry-After": "5"},
            ) from exc
        self._wakeup.set()
        return stored

    async def get(self, job_id: str) -> JobRecord | None:
        await self._maintain()
        return await self.job_store.get(job_id, datetime.now(UTC))

    async def cancel(self, job_id: str) -> JobRecord | None:
        await self._maintain()
        try:
            return await self.job_store.cancel(job_id, datetime.now(UTC))
        except JobConflictError as exc:
            raise RenderError(
                "job_already_running",
                "A running job cannot be cancelled safely.",
                409,
                False,
            ) from exc

    async def result(self, job: JobRecord) -> Artifact | None:
        if (
            job.status != "succeeded"
            or job.artifact_key is None
            or job.result_expires_at is None
            or job.result_expires_at <= datetime.now(UTC)
        ):
            return None
        return await self.artifact_store.get(job.artifact_key)

    async def _maintain(self, *, force: bool = False) -> None:
        loop = asyncio.get_running_loop()
        if not force and loop.time() < self._next_maintenance_at:
            return
        async with self._maintenance_lock:
            if not force and loop.time() < self._next_maintenance_at:
                return
            self._next_maintenance_at = loop.time() + self._maintenance_interval
            now = datetime.now(UTC)
            keys = await self.job_store.maintain(now)
            for key in keys:
                if await self._safe_delete(key):
                    try:
                        await self.job_store.acknowledge_artifact_deletion(key)
                    except Exception as exc:
                        logger.warning(
                            "artifact deletion acknowledgement retry error_type=%s",
                            type(exc).__name__,
                        )
            try:
                await self.artifact_store.maintain(now)
            except Exception as exc:
                logger.warning(
                    "artifact maintenance retry error_type=%s",
                    type(exc).__name__,
                )

    async def _safe_delete(self, key: str) -> bool:
        try:
            await self.artifact_store.delete(key)
            return True
        except Exception as exc:
            logger.warning(
                "artifact deletion retry error_type=%s",
                type(exc).__name__,
            )
            return False

    async def _worker(self, _index: int) -> None:
        try:
            while True:
                if self._closing:
                    return
                self._wakeup.clear()
                try:
                    await self._maintain()
                    job = await self.job_store.claim(
                        datetime.now(UTC),
                        self.settings.max_attempts,
                    )
                except Exception as exc:
                    logger.warning(
                        "async provider retry error_type=%s",
                        type(exc).__name__,
                    )
                    await asyncio.sleep(self.settings.poll_seconds)
                    continue
                if job is None:
                    try:
                        await asyncio.wait_for(
                            self._wakeup.wait(),
                            timeout=self.settings.poll_seconds,
                        )
                    except TimeoutError:
                        pass
                    continue
                try:
                    await self._process(job)
                except Exception as exc:
                    logger.error(
                        "async worker recovered error_type=%s",
                        type(exc).__name__,
                    )
                    await self._recover_claimed_job(job)
                if self._closing:
                    return
        except asyncio.CancelledError:
            raise

    async def _recover_claimed_job(self, job: JobRecord) -> None:
        await self._retry_state_transition(
            "requeue",
            lambda: self.job_store.requeue(
                job.id,
                job.attempt_count,
                datetime.now(UTC),
            ),
        )
        self._wakeup.set()

    async def _retry_state_transition(
        self,
        name: str,
        transition: Callable[[], Awaitable[None]],
    ) -> None:
        while True:
            try:
                await transition()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "async job transition retry transition=%s error_type=%s",
                    name,
                    type(exc).__name__,
                )
                await asyncio.sleep(self.settings.poll_seconds)

    async def _retry_success_transition(
        self,
        transition: Callable[[], Awaitable[JobRecord]],
    ) -> JobRecord:
        while True:
            try:
                return await transition()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "async job transition retry transition=succeed error_type=%s",
                    type(exc).__name__,
                )
                await asyncio.sleep(self.settings.poll_seconds)

    async def _process(self, job: JobRecord) -> None:
        stored_key: str | None = None
        try:
            payload = self.cipher.decrypt(job)
            rendered = await self.renderer(payload)
            stored = await self.artifact_store.put(
                job.id,
                rendered.body,
                media_type=rendered.media_type,
                filename=rendered.filename,
            )
            stored_key = stored.key
            now = datetime.now(UTC)
            result_expires_at = stored.expires_at
            queue_ms = max(
                0,
                round((job.started_at - job.created_at).total_seconds() * 1000)
                if job.started_at
                else 0,
            )
            settlement = asyncio.create_task(
                self._retry_success_transition(
                    lambda: self.job_store.succeed(
                        job.id,
                        expected_attempt=job.attempt_count,
                        artifact_key=stored_key,
                        media_type=rendered.media_type,
                        filename=rendered.filename,
                        artifact_bytes=len(rendered.body),
                        result_expires_at=result_expires_at,
                        queue_ms=queue_ms,
                        render_ms=max(
                            0,
                            round(
                                (now - job.started_at).total_seconds() * 1000
                            )
                            if job.started_at
                            else rendered.render_ms,
                        ),
                        now=now,
                    )
                )
            )
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError:
                settlement.cancel()
                await asyncio.gather(settlement, return_exceptions=True)
                raise
        except asyncio.CancelledError:
            # Leave the claimed row running. Startup recovery can distinguish
            # a committed success from interrupted work and enforce attempts.
            # An uploaded-but-uncommitted artifact retains its storage TTL.
            raise
        except Exception as exc:
            if stored_key:
                await self._safe_delete(stored_key)
            if self._closing:
                return
            if isinstance(exc, RenderError):
                code, message, retryable = exc.code, exc.message, exc.retryable
            else:
                code = "internal_error"
                message = "The async render could not be completed."
                retryable = True
            now = datetime.now(UTC)
            if (
                retryable
                and job.attempt_count < self.settings.max_attempts
                and now < job.queue_expires_at
            ):
                await self._retry_state_transition(
                    "requeue",
                    lambda: self.job_store.requeue(
                        job.id,
                        job.attempt_count,
                        now + timedelta(seconds=min(job.attempt_count, 5)),
                    ),
                )
                return
            await self._retry_state_transition(
                "fail",
                lambda: self.job_store.fail(
                    job.id,
                    expected_attempt=job.attempt_count,
                    code=code,
                    message=message,
                    retryable=retryable,
                    now=now,
                ),
            )
        finally:
            self._wakeup.set()


def public_job_document(job: JobRecord) -> dict[str, object]:
    result = None
    if job.status == "succeeded":
        result = {
            "media_type": job.media_type,
            "filename": job.filename,
            "bytes": job.artifact_bytes,
        }
    error = None
    if job.error_code:
        error = {
            "code": job.error_code,
            "message": job.error_message,
            "retryable": bool(job.error_retryable),
        }
    return {
        "id": job.id,
        "request_id": job.request_id,
        "status": job.status,
        "status_url": f"/v1/jobs/{job.id}",
        "result_url": (
            f"/v1/jobs/{job.id}/result"
            if job.status == "succeeded"
            else None
        ),
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": (
            job.completed_at.isoformat() if job.completed_at else None
        ),
        "result_expires_at": (
            job.result_expires_at.isoformat()
            if job.result_expires_at
            else None
        ),
        "attempts": job.attempt_count,
        "result": result,
        "timings": (
            {"queue_ms": job.queue_ms, "render_ms": job.render_ms}
            if job.queue_ms is not None and job.render_ms is not None
            else None
        ),
        "error": error,
    }
