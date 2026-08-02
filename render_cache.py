"""Bounded local render cache for exact self-hosted image requests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import stat
import tempfile

from render_contract import RenderRequest, canonical_render_document
from render_engine import RenderArtifact
from async_jobs import _ensure_private_directory


UTC = timezone.utc


class RenderCache:
    def __init__(
        self,
        directory: Path,
        *,
        ttl_seconds: int = 900,
        max_entries: int = 1_000,
        max_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.directory = directory
        self.ttl = timedelta(seconds=max(1, ttl_seconds))
        self.max_entries = max(1, max_entries)
        self.max_bytes = max(1, max_bytes)
        self.lock = asyncio.Lock()
        self._fingerprint_key: bytes | None = None

    async def start(self) -> None:
        if os.name == "nt":
            self.directory.mkdir(parents=True, exist_ok=True)
        else:
            _ensure_private_directory(self.directory)
        if os.name != "nt":
            os.chmod(self.directory, 0o700)
            info = self.directory.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                raise RuntimeError("render cache directory must be owner-only")
        self._fingerprint_key = await asyncio.to_thread(self._load_key)

    def _load_key(self) -> bytes:
        path = self.directory / ".fingerprint-key"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, create_flags, 0o600)
                material = os.urandom(32)
                try:
                    remaining = memoryview(material)
                    while remaining:
                        remaining = remaining[os.write(descriptor, remaining):]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                descriptor = os.open(path, flags)
            except FileExistsError:
                descriptor = os.open(path, flags)
        try:
            information = os.fstat(descriptor)
            if not stat.S_ISREG(information.st_mode) or (
                os.name != "nt" and information.st_mode & 0o077
            ):
                raise RuntimeError("render cache key must be an owner-only regular file")
            material = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        if len(material) != 32:
            raise RuntimeError("render cache key must contain exactly 32 bytes")
        return material

    def key(self, request: RenderRequest) -> str:
        if self._fingerprint_key is None:
            raise RuntimeError("render cache is not started")
        document = canonical_render_document(request)
        document["cache"] = False
        # Delivery affects notification, not pixels; never persist its URL in cache metadata.
        document["delivery"] = {"webhook_url": None}
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hmac.digest(
            self._fingerprint_key,
            canonical.encode("utf-8"),
            "sha256",
        ).hex()

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.directory / f"{key}.bin", self.directory / f"{key}.json"

    async def get(self, request: RenderRequest) -> RenderArtifact | None:
        key = self.key(request)
        body_path, metadata_path = self._paths(key)
        async with self.lock:
            return await asyncio.to_thread(self._read, body_path, metadata_path)

    def _read(self, body_path: Path, metadata_path: Path) -> RenderArtifact | None:
        try:
            metadata = json.loads(metadata_path.read_text("utf-8"))
            created = datetime.fromisoformat(metadata["created_at"])
            if datetime.now(UTC) - created > self.ttl:
                body_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                return None
            body = body_path.read_bytes()
            if sha256(body).hexdigest() != metadata["sha256"]:
                body_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                return None
            return RenderArtifact(
                body,
                metadata["media_type"],
                metadata["filename"],
                metadata.get("metadata", {}),
            )
        except (
            FileNotFoundError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            OSError,
        ):
            return None

    async def put(self, request: RenderRequest, artifact: RenderArtifact) -> None:
        key = self.key(request)
        body_path, metadata_path = self._paths(key)
        metadata = {
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "sha256": sha256(artifact.body).hexdigest(),
            "media_type": artifact.media_type,
            "filename": artifact.filename,
            "metadata": {
                name: artifact.metadata[name]
                for name in (
                    "width",
                    "height",
                    "navigation_status",
                    "blocked_subresources",
                    "output_count",
                )
                if name in artifact.metadata
            },
        }
        async with self.lock:
            await asyncio.to_thread(self._write, body_path, metadata_path, artifact.body, metadata)
            await asyncio.to_thread(self._trim)

    def _write(self, body_path: Path, metadata_path: Path, body: bytes, metadata: dict) -> None:
        descriptors = []
        paths = []
        try:
            for content, target in (
                (body, body_path),
                ((json.dumps(metadata, separators=(",", ":")) + "\n").encode(), metadata_path),
            ):
                descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=".cache-")
                descriptors.append(descriptor)
                paths.append(Path(temporary))
                with os.fdopen(descriptor, "wb") as stream:
                    descriptors.pop()
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
            for path in paths:
                path.unlink(missing_ok=True)

    def _trim(self) -> None:
        metadata_files = sorted(
            self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True
        )
        now = datetime.now(UTC)
        retained_bytes = 0
        for index, metadata_path in enumerate(metadata_files):
            remove = index >= self.max_entries
            if not remove:
                try:
                    document = json.loads(metadata_path.read_text("utf-8"))
                    remove = now - datetime.fromisoformat(document["created_at"]) > self.ttl
                    if not remove:
                        body_path = metadata_path.with_suffix(".bin")
                        entry_bytes = (
                            metadata_path.stat().st_size
                            + body_path.stat().st_size
                        )
                        if retained_bytes + entry_bytes > self.max_bytes:
                            remove = True
                        else:
                            retained_bytes += entry_bytes
                except Exception:
                    remove = True
            if remove:
                metadata_path.unlink(missing_ok=True)
                metadata_path.with_suffix(".bin").unlink(missing_ok=True)
