"""Bounded local render cache for exact self-hosted image requests."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import stat
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from .async_jobs import _ensure_private_directory, _sync_directory
from .render_contract import RenderRequest, canonical_render_document
from .render_engine import RenderArtifact, certification_public_key

UTC = timezone.utc


class RenderCache:
    def __init__(
        self,
        directory: Path,
        *,
        ttl_seconds: int = 900,
        max_entries: int = 1_000,
        max_bytes: int = 512 * 1024 * 1024,
        security_namespace: str = "",
    ) -> None:
        self.directory = directory
        self.ttl = timedelta(seconds=max(1, ttl_seconds))
        self.max_entries = max(1, max_entries)
        self.max_bytes = max(1, max_bytes)
        self.security_namespace = security_namespace
        self.lock = asyncio.Lock()
        self._fingerprint_key: bytes | None = None
        # In-memory index: stem -> (mtime, created_at, total_bytes)
        self._index: dict[str, tuple[float, datetime, int]] | None = None
        self._total_bytes: int = 0

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
        await self._run_locked(self._trim)

    def _load_key(self) -> bytes:
        path = self.directory / ".fingerprint-key"
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.directory,
                prefix=".fingerprint-key.",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            try:
                material = os.urandom(32)
                try:
                    remaining = memoryview(material)
                    while remaining:
                        remaining = remaining[os.write(descriptor, remaining):]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                try:
                    os.link(temporary, path)
                    _sync_directory(self.directory)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
            descriptor = os.open(path, flags)
        try:
            information = os.fstat(descriptor)
            if not stat.S_ISREG(information.st_mode) or (
                os.name != "nt" and information.st_mode & 0o077
            ):
                raise RuntimeError("render cache key must be an owner-only regular file")
            chunks = []
            remaining = 33
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            material = b"".join(chunks)
        finally:
            os.close(descriptor)
        if len(material) != 32:
            raise RuntimeError("render cache key must contain exactly 32 bytes")
        return material

    def key(self, request: RenderRequest, namespace: str | None = None) -> str:
        if self._fingerprint_key is None:
            raise RuntimeError("render cache is not started")
        document = canonical_render_document(request)
        document["cache"] = False
        # Delivery affects notification, not pixels; never persist its URL in cache metadata.
        document["delivery"] = {"webhook_url": None}
        if request.certification.enabled:
            secret = os.getenv("VIPERCAPTURE_CERTIFICATION_SECRET", "")
            document["certification_public_key"] = (
                certification_public_key(secret)
                if len(secret.encode()) >= 32
                else "disabled"
            )
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hmac.digest(
            self._fingerprint_key,
            self.security_namespace.encode("utf-8")
            + b"\0"
            + (namespace or "").encode("utf-8")
            + b"\0"
            + canonical.encode("utf-8"),
            "sha256",
        ).hex()

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.directory / f"{key}.bin", self.directory / f"{key}.json"

    async def get(
        self, request: RenderRequest, namespace: str | None = None
    ) -> RenderArtifact | None:
        key = self.key(request, namespace)
        body_path, metadata_path = self._paths(key)
        return await self._run_locked(self._read, body_path, metadata_path)

    async def _run_locked(self, operation, *args):
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await asyncio.shield(task)
                raise

    def _read(self, body_path: Path, metadata_path: Path) -> RenderArtifact | None:
        try:
            metadata = json.loads(metadata_path.read_text("utf-8"))
            created = datetime.fromisoformat(metadata["created_at"])
            if datetime.now(UTC) - created > self.ttl:
                self._drop_indexed(metadata_path.stem)
                body_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                return None
            body = body_path.read_bytes()
            if sha256(body).hexdigest() != metadata["sha256"]:
                self._drop_indexed(metadata_path.stem)
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

    def _drop_indexed(self, stem: str) -> None:
        if self._index is not None:
            previous = self._index.pop(stem, None)
            if previous is not None:
                self._total_bytes = max(0, self._total_bytes - previous[2])

    async def put(
        self,
        request: RenderRequest,
        artifact: RenderArtifact,
        namespace: str | None = None,
    ) -> None:
        key = self.key(request, namespace)
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
                    "encode_ms",
                )
                if name in artifact.metadata
            },
        }
        await self._run_locked(
            self._write_and_trim,
            body_path,
            metadata_path,
            artifact.body,
            metadata,
        )

    def _write_and_trim(
        self,
        body_path: Path,
        metadata_path: Path,
        body: bytes,
        metadata: dict,
    ) -> None:
        self._write(body_path, metadata_path, body, metadata)
        if self._index is None:
            self._trim()
            return
        stem = metadata_path.stem
        self._drop_indexed(stem)
        try:
            meta_stat = metadata_path.stat()
            entry_bytes = meta_stat.st_size + body_path.stat().st_size
            created = datetime.fromisoformat(metadata["created_at"])
            self._index[stem] = (meta_stat.st_mtime, created, entry_bytes)
            self._total_bytes += entry_bytes
        except OSError:
            self._trim()
            return
        self._evict_indexed()

    def _evict_indexed(self) -> None:
        if self._index is None:
            return
        now = datetime.now(UTC)
        expired = [
            stem for stem, (_mtime, created, _size) in self._index.items()
            if now - created > self.ttl
        ]
        for stem in expired:
            self._drop_indexed(stem)
            (self.directory / f"{stem}.json").unlink(missing_ok=True)
            (self.directory / f"{stem}.bin").unlink(missing_ok=True)
        if len(self._index) <= self.max_entries and self._total_bytes <= self.max_bytes:
            return
        # Sort newest-first so we retain the most recent entries within limits.
        ordered = sorted(self._index.items(), key=lambda item: item[1][0], reverse=True)
        retained_bytes = 0
        new_index: dict[str, tuple[float, datetime, int]] = {}
        for idx, (stem, entry) in enumerate(ordered):
            if idx >= self.max_entries or retained_bytes + entry[2] > self.max_bytes:
                (self.directory / f"{stem}.json").unlink(missing_ok=True)
                (self.directory / f"{stem}.bin").unlink(missing_ok=True)
            else:
                new_index[stem] = entry
                retained_bytes += entry[2]
        self._index = new_index
        self._total_bytes = retained_bytes

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
        json_entries: list[tuple[str, float, Path, int]] = []
        bin_sizes: dict[str, int] = {}
        bin_paths: dict[str, Path] = {}
        with os.scandir(self.directory) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith(".fingerprint-key.") and name.endswith(".tmp"):
                    Path(entry.path).unlink(missing_ok=True)
                    continue
                if name.startswith(".cache-"):
                    Path(entry.path).unlink(missing_ok=True)
                    continue
                if name.endswith(".bin"):
                    stem = name[:-4]
                    try:
                        bin_sizes[stem] = entry.stat().st_size
                        bin_paths[stem] = Path(entry.path)
                    except OSError:
                        pass
                elif name.endswith(".json"):
                    stem = name[:-5]
                    try:
                        st = entry.stat()
                        json_entries.append((stem, st.st_mtime, Path(entry.path), st.st_size))
                    except OSError:
                        pass

        json_stems = {stem for stem, _mtime, _path, _size in json_entries}
        for stem, body_path in bin_paths.items():
            if stem not in json_stems:
                body_path.unlink(missing_ok=True)

        json_entries.sort(key=lambda item: item[1], reverse=True)
        now = datetime.now(UTC)
        retained_bytes = 0
        index: dict[str, tuple[float, datetime, int]] = {}
        for idx, (stem, mtime, metadata_path, meta_size) in enumerate(json_entries):
            remove = idx >= self.max_entries or stem not in bin_sizes
            created: datetime | None = None
            entry_bytes = 0
            if not remove:
                try:
                    document = json.loads(metadata_path.read_text("utf-8"))
                    created = datetime.fromisoformat(document["created_at"])
                    remove = now - created > self.ttl
                    if not remove:
                        entry_bytes = meta_size + bin_sizes[stem]
                        if retained_bytes + entry_bytes > self.max_bytes:
                            remove = True
                        else:
                            retained_bytes += entry_bytes
                except Exception:
                    remove = True
            if remove:
                metadata_path.unlink(missing_ok=True)
                (bin_paths.get(stem) or metadata_path.with_suffix(".bin")).unlink(missing_ok=True)
            elif created is not None:
                index[stem] = (mtime, created, entry_bytes)
        self._index = index
        self._total_bytes = retained_bytes
