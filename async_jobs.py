"""Provider-neutral durable async render jobs."""

from __future__ import annotations

import asyncio
import hmac
import importlib
import inspect
import json
import logging
import os
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from render_contract import RenderRequest, canonical_render_document
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
    request_fingerprint: bytes | None = None
    webhook_payload: bytes | None = None
    webhook_event_status: str | None = None
    webhook_attempt_count: int = 0
    webhook_available_at: datetime | None = None
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


class IdempotencyConflictError(Exception):
    pass


class ArtifactExpiredError(Exception):
    pass


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    target = Path(os.path.abspath(path))
    if target == Path(target.anchor):
        raise RuntimeError("async job data directory cannot be a filesystem root")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current = Path(target.anchor)
    descriptor = os.open(current, flags)
    try:
        for component in target.parts[1:]:
            child = current / component
            try:
                child_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                else:
                    _sync_directory(current)
                try:
                    child_descriptor = os.open(
                        component,
                        flags,
                        dir_fd=descriptor,
                    )
                except OSError as exc:
                    raise RuntimeError(
                        "async job data path must contain only directories"
                    ) from exc
            except OSError as exc:
                raise RuntimeError(
                    "async job data path must contain only directories"
                ) from exc
            information = os.fstat(child_descriptor)
            mode = stat.S_IMODE(information.st_mode)
            trusted_sticky_directory = (
                information.st_uid == 0
                and bool(information.st_mode & stat.S_ISVTX)
            )
            if (
                information.st_uid not in {0, os.getuid()}
                or (
                    mode & 0o022
                    and not trusted_sticky_directory
                    and child != target
                )
            ):
                os.close(child_descriptor)
                raise RuntimeError(
                    "async job data path contains an unsafe directory"
                )
            os.close(descriptor)
            descriptor = child_descriptor
            current = child

        information = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(information.st_mode)
            or information.st_uid != os.getuid()
        ):
            raise RuntimeError(
                "async job data directory must be owner-controlled"
            )
        os.fchmod(descriptor, 0o700)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            raise RuntimeError(
                "async job data directory must be owner-only"
            )
    finally:
        os.close(descriptor)


def _read_private_key(path: Path) -> bytes:
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
        raise RuntimeError("async job key must be a private regular file") from exc
    try:
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != os.getuid()
            or information.st_mode & 0o077
        ):
            raise RuntimeError(
                "async job key must be an owner-only regular file"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


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
        claim_token: str,
    ) -> JobRecord | None: ...
    async def get_by_request_id(self, request_id: str) -> JobRecord | None: ...
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
    ) -> JobRecord: ...
    async def fail(
        self,
        job_id: str,
        *,
        expected_attempt: int,
        code: str,
        message: str,
        retryable: bool,
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
    async def expire_result(
        self,
        job_id: str,
        artifact_key: str,
        now: datetime,
    ) -> JobRecord | None: ...
    async def acknowledge_artifact_deletion(self, key: str) -> None: ...
    async def pending_notifications(
        self, now: datetime, limit: int = 100
    ) -> list[JobRecord]: ...
    async def defer_notification(
        self,
        job_id: str,
        webhook_payload: bytes,
        available_at: datetime,
    ) -> None: ...
    async def acknowledge_notification(
        self, job_id: str, webhook_payload: bytes
    ) -> None: ...


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
JobNotifier = Callable[[str, JobRecord], Awaitable[None]]


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
    if not artifact_spec and os.getenv("VIPERCAPTURE_S3_BUCKET"):
        artifact_spec = "s3_artifact_store:create_s3_artifact_store"
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
    if os.name == "nt":
        raise RuntimeError(
            "VIPERCAPTURE_JOB_SECRET is required for async jobs on Windows"
        )
    _ensure_private_directory(data_dir)
    path = data_dir / "async-jobs.key"
    try:
        material = _read_private_key(path)
    except FileNotFoundError:
        material = os.urandom(32)
        temporary = data_dir / f".async-jobs.key.{uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(temporary, flags, 0o600)
            try:
                view = memoryview(material)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.link(temporary, path)
                _sync_directory(data_dir)
            except FileExistsError:
                material = _read_private_key(path)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
    if len(material) < 32:
        raise RuntimeError("async job key must contain at least 32 bytes")
    return sha256(PAYLOAD_VERSION + material).digest()


class PayloadCipher:
    def __init__(self, key: bytes) -> None:
        self._cipher = AESGCM(key)
        self._fingerprint_key = sha256(
            b"vipercapture-request-fingerprint\0" + key
        ).digest()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "PayloadCipher":
        return cls(_read_or_create_key(data_dir))

    def encrypt(self, job_id: str, payload: RenderRequest) -> bytes:
        nonce = os.urandom(12)
        plaintext = self._serialize(payload)
        return nonce + self._cipher.encrypt(
            nonce,
            plaintext,
            job_id.encode("ascii"),
        )

    def fingerprint(self, payload: RenderRequest) -> bytes:
        return hmac.digest(
            self._fingerprint_key,
            self._serialize(payload),
            "sha256",
        )

    def encrypt_webhook(self, job_id: str, url: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._cipher.encrypt(
            nonce,
            url.encode("utf-8"),
            job_id.encode("ascii") + b"\0webhook",
        )

    def decrypt_webhook(self, job: JobRecord) -> str:
        encrypted = job.webhook_payload
        if encrypted is None or len(encrypted) <= 28:
            raise ValueError("webhook payload is unavailable")
        nonce, ciphertext = encrypted[:12], encrypted[12:]
        plaintext = self._cipher.decrypt(
            nonce,
            ciphertext,
            job.id.encode("ascii") + b"\0webhook",
        )
        return plaintext.decode("utf-8")

    @staticmethod
    def _serialize(payload: RenderRequest) -> bytes:
        return json.dumps(
            canonical_render_document(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

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
        notifier: JobNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.job_store = job_store
        self.artifact_store = artifact_store
        self.renderer = renderer
        self.cipher = cipher or PayloadCipher.for_data_dir(settings.data_dir)
        self.notifier = notifier
        self._wakeups = [
            asyncio.Event() for _ in range(settings.worker_count)
        ]
        self._workers: list[asyncio.Task] = []
        self._wake_tasks: set[asyncio.Task] = set()
        self._closing = False
        self._maintenance_lock = asyncio.Lock()
        self._notification_lock = asyncio.Lock()
        self._notification_wakeup = asyncio.Event()
        self._notification_task: asyncio.Task | None = None
        self._next_maintenance_at = 0.0
        self._maintenance_interval = max(1.0, settings.poll_seconds)

    async def start(self) -> None:
        self._closing = False
        job_store_started = False
        artifact_store_started = False
        try:
            job_store_started = True
            await self.job_store.start()
            artifact_store_started = True
            await self.artifact_store.start()
            await self.job_store.requeue_running(
                datetime.now(UTC),
                self.settings.max_attempts,
            )
            await self._maintain(force=True)
            if self.notifier is not None:
                self._notification_wakeup.set()
                self._notification_task = asyncio.create_task(
                    self._notification_worker(),
                    name="vipercapture-webhook-worker",
                )
            self._workers = [
                asyncio.create_task(
                    self._worker(index),
                    name=f"vipercapture-job-worker-{index + 1}",
                )
                for index in range(self.settings.worker_count)
            ]
        except BaseException:
            if artifact_store_started:
                with suppress(Exception):
                    await self._close_provider(self.artifact_store)
            if job_store_started:
                with suppress(Exception):
                    await self._close_provider(self.job_store)
            raise

    async def close(self) -> None:
        self._closing = True
        if self._notification_task is not None:
            self._notification_task.cancel()
            await asyncio.gather(
                self._notification_task, return_exceptions=True
            )
            self._notification_task = None
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            done, pending = await asyncio.wait(
                set(self._workers),
                timeout=1.0,
            )
            for worker in done:
                self._consume_task_result(worker)
            for worker in pending:
                worker.add_done_callback(self._consume_task_result)
        self._workers.clear()
        for task in self._wake_tasks:
            task.cancel()
        await asyncio.gather(*self._wake_tasks, return_exceptions=True)
        self._wake_tasks.clear()
        try:
            await self._close_provider(self.artifact_store)
        finally:
            await self._close_provider(self.job_store)

    async def _close_provider(self, provider: object) -> None:
        close_task = asyncio.create_task(provider.close())
        done, _pending = await asyncio.wait({close_task}, timeout=1.0)
        if close_task in done:
            close_task.result()
        else:
            close_task.add_done_callback(self._consume_task_result)

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
            request_fingerprint=self.cipher.fingerprint(payload),
            webhook_payload=(
                self.cipher.encrypt_webhook(
                    job_id, str(payload.delivery.webhook_url)
                )
                if payload.delivery.webhook_url is not None
                else None
            ),
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
        except IdempotencyConflictError as exc:
            raise RenderError(
                "idempotency_key_conflict",
                "X-Request-Id was already used for a different render.",
                409,
                False,
            ) from exc
        self._wake_workers()
        return stored

    async def existing(
        self,
        payload: RenderRequest,
        *,
        request_id: str,
    ) -> JobRecord | None:
        """Return an idempotent replay before repeating external validation."""
        await self._maintain()
        current = await self.job_store.get_by_request_id(request_id)
        if current is None:
            return None
        current = await self.job_store.get(current.id, datetime.now(UTC))
        if current is None:
            return None
        fingerprint = self.cipher.fingerprint(payload)
        if (
            current.request_fingerprint is None
            or not hmac.compare_digest(
                current.request_fingerprint,
                fingerprint,
            )
        ):
            raise RenderError(
                "idempotency_key_conflict",
                "X-Request-Id was already used for a different render.",
                409,
                False,
            )
        return current

    async def get(self, job_id: str) -> JobRecord | None:
        await self._maintain()
        return await self.job_store.get(job_id, datetime.now(UTC))

    async def cancel(self, job_id: str) -> JobRecord | None:
        await self._maintain()
        try:
            job = await self.job_store.cancel(job_id, datetime.now(UTC))
            if job is not None and job.webhook_event_status is not None:
                self._notification_wakeup.set()
            return job
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
        ):
            return None
        now = datetime.now(UTC)
        if job.result_expires_at <= now:
            await self._expire_result(job, now, delete_artifact=True)
            return None
        artifact = await self.artifact_store.get(job.artifact_key)
        now = datetime.now(UTC)
        if artifact is None or job.result_expires_at <= now:
            await self._expire_result(
                job,
                now,
                delete_artifact=artifact is not None,
            )
            return None
        return artifact

    async def _expire_result(
        self,
        job: JobRecord,
        now: datetime,
        *,
        delete_artifact: bool,
    ) -> None:
        if job.artifact_key is None:
            return
        expired = await self.job_store.expire_result(
            job.id,
            job.artifact_key,
            now,
        )
        if (
            not delete_artifact
            or expired is None
            or expired.status != "expired"
            or expired.artifact_key != job.artifact_key
            or not await self._safe_delete(job.artifact_key)
        ):
            return
        try:
            await self.job_store.acknowledge_artifact_deletion(
                job.artifact_key
            )
        except Exception as exc:
            logger.warning(
                "artifact deletion acknowledgement retry error_type=%s",
                type(exc).__name__,
            )

    async def _maintain(self, *, force: bool = False) -> None:
        if self._closing:
            return
        loop = asyncio.get_running_loop()
        if not force and loop.time() < self._next_maintenance_at:
            return
        async with self._maintenance_lock:
            if not force and loop.time() < self._next_maintenance_at:
                return
            self._next_maintenance_at = loop.time() + self._maintenance_interval
            now = datetime.now(UTC)
            try:
                keys = await self.job_store.maintain(now)
            except Exception as exc:
                logger.warning(
                    "job maintenance retry error_type=%s",
                    type(exc).__name__,
                )
                keys = []
            if self._closing:
                return
            for key in keys:
                if self._closing:
                    return
                if await self._safe_delete(key):
                    if self._closing:
                        return
                    try:
                        await self.job_store.acknowledge_artifact_deletion(key)
                    except Exception as exc:
                        logger.warning(
                            "artifact deletion acknowledgement retry error_type=%s",
                            type(exc).__name__,
                        )
                    if self._closing:
                        return
            if self._closing:
                return
            try:
                await self.artifact_store.maintain(now)
            except Exception as exc:
                logger.warning(
                    "artifact maintenance retry error_type=%s",
                    type(exc).__name__,
                )
    async def _drain_notifications(self) -> None:
        if self.notifier is None or self._closing:
            return
        async with self._notification_lock:
            try:
                pending = await self.job_store.pending_notifications(
                    datetime.now(UTC)
                )
            except Exception as exc:
                logger.warning(
                    "webhook outbox lookup failed error_type=%s",
                    type(exc).__name__,
                )
                return
            for job in pending:
                if self._closing or job.webhook_payload is None:
                    return
                try:
                    url = self.cipher.decrypt_webhook(job)
                except Exception as exc:
                    logger.error(
                        "webhook outbox payload invalid job_id=%s error_type=%s",
                        job.id,
                        type(exc).__name__,
                    )
                    await self.job_store.acknowledge_notification(
                        job.id, job.webhook_payload
                    )
                    continue
                try:
                    event_job = (
                        replace(job, status=job.webhook_event_status)
                        if job.webhook_event_status is not None
                        else job
                    )
                    await self.notifier(url, event_job)
                except Exception as exc:
                    logger.warning(
                        "async job webhook delivery failed job_id=%s error_type=%s",
                        job.id,
                        type(exc).__name__,
                    )
                    delay = min(
                        3600,
                        0.1 * (2 ** min(job.webhook_attempt_count, 15)),
                    )
                    await self.job_store.defer_notification(
                        job.id,
                        job.webhook_payload,
                        datetime.now(UTC) + timedelta(seconds=delay),
                    )
                    continue
                await self.job_store.acknowledge_notification(
                    job.id, job.webhook_payload
                )

    async def _notification_worker(self) -> None:
        try:
            while not self._closing:
                self._notification_wakeup.clear()
                try:
                    await self._drain_notifications()
                except Exception as exc:
                    logger.warning(
                        "webhook outbox worker retry error_type=%s",
                        type(exc).__name__,
                    )
                try:
                    await asyncio.wait_for(
                        self._notification_wakeup.wait(),
                        timeout=self.settings.poll_seconds,
                    )
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

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
        wakeup = self._wakeups[_index]
        try:
            while True:
                if self._closing:
                    return
                wakeup.clear()
                try:
                    await self._maintain()
                    claim_token = str(uuid4())
                    job = await self._claim_with_retry(claim_token)
                except Exception as exc:
                    logger.warning(
                        "async provider retry error_type=%s",
                        type(exc).__name__,
                    )
                    await asyncio.sleep(self.settings.poll_seconds)
                    continue
                if self._closing:
                    return
                if job is None:
                    try:
                        await asyncio.wait_for(
                            wakeup.wait(),
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
            lambda: self._transition_conflict_resolved(job),
        )
        self._wake_workers()

    def _wake_workers(self) -> None:
        for wakeup in self._wakeups:
            wakeup.set()

    async def _claim_with_retry(self, claim_token: str) -> JobRecord | None:
        while True:
            if self._closing:
                return None
            try:
                return await self.job_store.claim(
                    datetime.now(UTC),
                    self.settings.max_attempts,
                    claim_token,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closing:
                    return None
                logger.warning(
                    "async claim retry error_type=%s",
                    type(exc).__name__,
                )
                await asyncio.sleep(self.settings.poll_seconds)

    def _wake_at(self, available_at: datetime) -> None:
        async def wake() -> None:
            delay = max(
                0.0,
                (available_at - datetime.now(UTC)).total_seconds(),
            )
            await asyncio.sleep(delay)
            self._wake_workers()

        task = asyncio.create_task(wake())
        self._wake_tasks.add(task)
        task.add_done_callback(self._wake_tasks.discard)

    async def _retry_state_transition(
        self,
        name: str,
        transition: Callable[[], Awaitable[None]],
        reconcile: Callable[[], Awaitable[bool]],
        *,
        settle_on_cancel: bool = False,
    ) -> None:
        try:
            while True:
                if self._closing:
                    return
                try:
                    await transition()
                    return
                except asyncio.CancelledError:
                    raise
                except JobConflictError:
                    while True:
                        if self._closing:
                            return
                        try:
                            if await reconcile():
                                return
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "async job transition conflict reconciliation "
                                "transition=%s error_type=%s",
                                name,
                                type(exc).__name__,
                            )
                        await asyncio.sleep(self.settings.poll_seconds)
                except Exception as exc:
                    if self._closing:
                        return
                    logger.warning(
                        "async job transition retry transition=%s error_type=%s",
                        name,
                        type(exc).__name__,
                    )
                    await asyncio.sleep(self.settings.poll_seconds)
        except asyncio.CancelledError:
            if settle_on_cancel:
                final_task = asyncio.create_task(transition())
                done, _pending = await asyncio.wait(
                    {final_task},
                    timeout=max(
                        0.1,
                        min(1.0, self.settings.poll_seconds),
                    ),
                )
                if final_task not in done:
                    final_task.add_done_callback(self._consume_task_result)
                else:
                    try:
                        final_task.result()
                    except Exception as exc:
                        logger.warning(
                            "async job final transition failed "
                            "transition=%s error_type=%s",
                            name,
                            type(exc).__name__,
                        )
            raise

    async def _transition_conflict_resolved(self, job: JobRecord) -> bool:
        current = await self.job_store.get(job.id, datetime.now(UTC))
        return current is None or (
            current.status != "running"
            or current.attempt_count != job.attempt_count
        )

    @staticmethod
    def _consume_task_result(task: asyncio.Task) -> None:
        with suppress(BaseException):
            task.result()

    async def _retry_success_transition(
        self,
        transition: Callable[[], Awaitable[JobRecord]],
        reconcile: Callable[[], Awaitable[JobRecord | None]],
        expected_attempt: int,
        expires_at: datetime,
    ) -> tuple[JobRecord | None, bool]:
        while True:
            if self._closing:
                raise asyncio.CancelledError
            try:
                return await transition(), False
            except asyncio.CancelledError:
                raise
            except JobConflictError:
                while True:
                    if self._closing:
                        raise asyncio.CancelledError
                    try:
                        current = await reconcile()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "async job success conflict reconciliation "
                            "error_type=%s",
                            type(exc).__name__,
                        )
                    else:
                        if (
                            current is None
                            or current.status != "running"
                            or current.attempt_count != expected_attempt
                        ):
                            return current, True
                    await asyncio.sleep(self.settings.poll_seconds)
            except ArtifactExpiredError as exc:
                raise RenderError(
                    "async_result_expired",
                    "The async result expired before settlement.",
                    410,
                    False,
                ) from exc
            except Exception as exc:
                if self._closing:
                    raise asyncio.CancelledError
                logger.warning(
                    "async job transition retry transition=succeed error_type=%s",
                    type(exc).__name__,
                )
                remaining = (expires_at - datetime.now(UTC)).total_seconds()
                await asyncio.sleep(
                    (
                        min(self.settings.poll_seconds, remaining)
                        if remaining > 0
                        else self.settings.poll_seconds
                    )
                )

    async def _process(self, job: JobRecord) -> None:
        stored_key: str | None = None
        payload: RenderRequest | None = None
        try:
            payload = self.cipher.decrypt(job)
            rendered = await self.renderer(payload)
            if self._closing:
                return
            stored = await self.artifact_store.put(
                job.id,
                rendered.body,
                media_type=rendered.media_type,
                filename=rendered.filename,
            )
            stored_key = stored.key
            if self._closing:
                return
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
                    ),
                    lambda: self.job_store.get(job.id, datetime.now(UTC)),
                    job.attempt_count,
                    result_expires_at,
                )
            )
            try:
                settled, reconciled = await asyncio.shield(settlement)
            except asyncio.CancelledError:
                settlement.cancel()
                done, _pending = await asyncio.wait(
                    {settlement},
                    timeout=1.0,
                )
                if settlement not in done:
                    settlement.add_done_callback(self._consume_task_result)
                raise
            if (
                reconciled
                and (
                    settled is None
                    or settled.status != "succeeded"
                    or settled.artifact_key != stored_key
                )
            ):
                await self._safe_delete(stored_key)
                stored_key = None
                return
            if settled is not None:
                self._notification_wakeup.set()
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
                    lambda: self._transition_conflict_resolved(job),
                )
                self._wake_at(
                    now + timedelta(seconds=min(job.attempt_count, 5))
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
                ),
                lambda: self._transition_conflict_resolved(job),
                settle_on_cancel=not retryable,
            )
            self._notification_wakeup.set()
        finally:
            self._wake_workers()

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
