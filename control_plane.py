"""Small self-hosted control plane: projects, keys, ownership, profiles, and metrics."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager, suppress
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from async_jobs import _ensure_private_directory

MAX_BASELINES_PER_PROJECT = 100
MAX_BASELINE_BYTES_PER_PROJECT = 512 * 1024 * 1024
MAX_PROFILES_PER_PROJECT = 100
MAX_PROFILE_BYTES_PER_PROJECT = 512 * 1024 * 1024
MAX_SCHEDULES_PER_PROJECT = 100
MAX_SCHEDULE_BYTES_PER_PROJECT = 512 * 1024 * 1024
MAX_AUDIT_EVENTS = 100_000
LIMIT_LEASE_SECONDS = 15 * 60


class BaselineQuotaError(RuntimeError):
    pass


class ProfileQuotaError(RuntimeError):
    pass


class ScheduleQuotaError(RuntimeError):
    pass


async def _settled_thread(operation, *args):
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await asyncio.shield(task)
        raise


def _metric_label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)

    def inc(self, name: str, value: float = 1, **labels: object) -> None:
        key = (name, tuple(sorted((key, str(value)) for key, value in labels.items())))
        with self._lock:
            self._counters[key] += value

    def gauge(self, name: str, value: float, **labels: object) -> None:
        key = (name, tuple(sorted((key, str(value)) for key, value in labels.items())))
        with self._lock:
            self._gauges[key] = value

    def prometheus(self) -> str:
        lines = []
        with self._lock:
            values = {**self._counters, **self._gauges}
        for (name, labels), value in sorted(values.items()):
            suffix = ""
            if labels:
                suffix = "{" + ",".join(f'{key}="{_metric_label(item)}"' for key, item in labels) + "}"
            lines.append(f"vipercapture_{name}{suffix} {value:g}")
        return "\n".join(lines) + "\n"


class ControlPlane:
    def __init__(self, path: Path, *, encryption_secret: str) -> None:
        self.path = path
        secret = encryption_secret.encode()
        self._cipher = AESGCM(hashlib.sha256(secret).digest())
        self._key_hash_secret = hashlib.sha256(b"vipercapture-api-key\0" + secret).digest()
        self._leases: dict[str, deque[str]] = defaultdict(deque)
        self._limit_lock = asyncio.Lock()

    async def _settled_acquisition(self, operation, *args):
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            result = await asyncio.shield(task)
            lease_id = result[1]
            if lease_id is not None:
                await _settled_thread(self._release, lease_id)
            raise

    def initialize(self) -> None:
        if os.name == "nt":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        else:
            _ensure_private_directory(self.path.parent)
            self._secure_file(self.path, create=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, rpm INTEGER NOT NULL,
                    concurrency INTEGER NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, key_hash BLOB UNIQUE NOT NULL,
                    prefix TEXT NOT NULL, name TEXT NOT NULL, scopes TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    revoked_at INTEGER, FOREIGN KEY(project_id) REFERENCES projects(id)
                );
                CREATE TABLE IF NOT EXISTS resources (
                    kind TEXT NOT NULL, id TEXT NOT NULL, project_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT 0, expires_at INTEGER,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(kind, id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, actor TEXT NOT NULL,
                    action TEXT NOT NULL, resource TEXT, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT NOT NULL, project_id TEXT NOT NULL, payload BLOB NOT NULL,
                    updated_at INTEGER NOT NULL, expires_at INTEGER,
                    PRIMARY KEY(id, project_id)
                );
                CREATE TABLE IF NOT EXISTS baselines (
                    name TEXT NOT NULL, project_id TEXT NOT NULL, body BLOB NOT NULL,
                    sha256 TEXT NOT NULL, updated_at INTEGER NOT NULL,
                    PRIMARY KEY(name, project_id)
                );
                CREATE TABLE IF NOT EXISTS rate_events (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rate_events_project_time
                    ON rate_events(project_id, created_at);
                CREATE INDEX IF NOT EXISTS rate_events_time
                    ON rate_events(created_at);
                CREATE TABLE IF NOT EXISTS active_leases (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS active_leases_project
                    ON active_leases(project_id);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(api_keys)")}
            if "scopes" not in columns:
                db.execute(
                    "ALTER TABLE api_keys ADD COLUMN scopes TEXT NOT NULL DEFAULT '[\"render\",\"jobs\",\"schedules\",\"profiles\",\"baselines\"]'"
                )
            resource_columns = {
                row[1] for row in db.execute("PRAGMA table_info(resources)")
            }
            if "created_at" not in resource_columns:
                db.execute(
                    "ALTER TABLE resources ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0"
                )
            if "expires_at" not in resource_columns:
                db.execute("ALTER TABLE resources ADD COLUMN expires_at INTEGER")
            if "size_bytes" not in resource_columns:
                db.execute(
                    "ALTER TABLE resources ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _secure_file(path: Path, *, create: bool = False) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if create:
            flags |= os.O_CREAT
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError(
                "control database files must be owner-controlled regular files"
            ) from exc
        try:
            information = os.fstat(descriptor)
            if (
                not stat.S_ISREG(information.st_mode)
                or information.st_uid != os.getuid()
            ):
                raise RuntimeError(
                    "control database files must be owner-controlled regular files"
                )
            os.fchmod(descriptor, 0o600)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise RuntimeError("control database files must be owner-only")
        finally:
            os.close(descriptor)

    def _secure_files(self) -> None:
        self._secure_file(self.path, create=True)
        self._secure_file(Path(f"{self.path}-wal"))
        self._secure_file(Path(f"{self.path}-shm"))

    @contextmanager
    def _connect(self):
        self._secure_files()
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            try:
                self._secure_files()
            finally:
                connection.close()

    def create_project(self, name: str, rpm: int, concurrency: int) -> dict[str, object]:
        project_id = secrets.token_hex(12)
        created_at = int(time.time())
        with self._connect() as db:
            db.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
                (project_id, name, rpm, concurrency, created_at),
            )
        return {"id": project_id, "name": name, "requests_per_minute": rpm, "concurrency": concurrency, "created_at": created_at}

    def list_projects(self) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY created_at, id").fetchall()
        projects = []
        for row in rows:
            project = dict(row)
            project["requests_per_minute"] = project.pop("rpm")
            projects.append(project)
        return projects

    def create_key(self, project_id: str, name: str, scopes: list[str] | None = None) -> dict[str, object]:
        key_id = secrets.token_hex(12)
        prefix = secrets.token_hex(4)
        raw = f"vcp_{prefix}_{secrets.token_urlsafe(32)}"
        scopes = scopes or ["render", "jobs", "schedules", "profiles", "baselines"]
        with self._connect() as db:
            if db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise KeyError(project_id)
            db.execute(
                "INSERT INTO api_keys(id,project_id,key_hash,prefix,name,scopes,created_at,revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (key_id, project_id, self._key_digest(raw), prefix, name, json.dumps(scopes), int(time.time())),
            )
        return {"id": key_id, "project_id": project_id, "name": name, "prefix": prefix, "scopes": scopes, "api_key": raw}

    def revoke_key(self, key_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (int(time.time()), key_id))
        return cursor.rowcount == 1

    def authenticate(self, raw: str) -> dict[str, object] | None:
        digest = self._key_digest(raw)
        with self._connect() as db:
            row = db.execute(
                "SELECT k.id key_id, k.project_id, k.scopes, p.rpm, p.concurrency FROM api_keys k JOIN projects p ON p.id=k.project_id WHERE k.key_hash=? AND k.revoked_at IS NULL",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["scopes"] = json.loads(str(result["scopes"]))
        return result

    def _key_digest(self, raw: str) -> bytes:
        # API keys are generated 256-bit tokens, not human passwords. A keyed,
        # constant-time digest is intentional; a password KDF would enable auth DoS.
        return hmac.digest(  # lgtm[py/weak-sensitive-data-hashing]
            self._key_hash_secret, raw.encode(), "sha256"
        )

    async def acquire(
        self, identity: dict[str, object], *, concurrency: bool = True
    ) -> tuple[bool, str | None]:
        project_id = str(identity["project_id"])
        async with self._limit_lock:
            result, lease_id = await self._settled_acquisition(
                self._acquire, identity, project_id, concurrency
            )
            if result[0] is False:
                return result
            if lease_id is not None:
                self._leases[project_id].append(lease_id)
            return result

    def _acquire(
        self,
        identity: dict[str, object],
        project_id: str,
        concurrency: bool,
    ) -> tuple[tuple[bool, str | None], str | None]:
        now = time.time()
        lease_id = secrets.token_hex(16)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM rate_events WHERE created_at<=?", (now - 60,))
            db.execute("DELETE FROM active_leases WHERE expires_at<=?", (now,))
            rpm = db.execute(
                "SELECT count(*) FROM rate_events WHERE project_id=?", (project_id,)
            ).fetchone()[0]
            if int(rpm) >= int(identity["rpm"]):
                return (False, "rate_limit_exceeded"), None
            if concurrency:
                active = db.execute(
                    "SELECT count(*) FROM active_leases WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
                if int(active) >= int(identity["concurrency"]):
                    return (False, "concurrency_limit_exceeded"), None
            db.execute(
                "INSERT INTO rate_events VALUES (?, ?, ?)",
                (secrets.token_hex(16), project_id, now),
            )
            if concurrency:
                db.execute(
                    "INSERT INTO active_leases VALUES (?, ?, ?)",
                    (lease_id, project_id, now + LIMIT_LEASE_SECONDS),
                )
        return (True, None), lease_id if concurrency else None

    async def release(self, project_id: str) -> None:
        async with self._limit_lock:
            if not self._leases[project_id]:
                return
            lease_id = self._leases[project_id].popleft()
            await _settled_thread(self._release, lease_id)

    def _release(self, lease_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM active_leases WHERE id=?", (lease_id,))

    async def acquire_worker(self, project_id: str) -> bool:
        async with self._limit_lock:
            acquired, lease_id = await self._settled_acquisition(
                self._acquire_worker, project_id
            )
            if not acquired:
                return False
            self._leases[project_id].append(lease_id)
            return True

    def _acquire_worker(self, project_id: str) -> tuple[bool, str | None]:
        now = time.time()
        lease_id = secrets.token_hex(16)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM active_leases WHERE expires_at<=?", (now,))
            row = db.execute(
                "SELECT concurrency FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if row is None:
                return False, None
            active = db.execute(
                "SELECT count(*) FROM active_leases WHERE project_id=?", (project_id,)
            ).fetchone()[0]
            if int(active) >= int(row["concurrency"]):
                return False, None
            db.execute(
                "INSERT INTO active_leases VALUES (?, ?, ?)",
                (lease_id, project_id, now + LIMIT_LEASE_SECONDS),
            )
        return True, lease_id

    def own(
        self,
        kind: str,
        resource_id: str,
        project_id: str,
        ttl_seconds: int | None = None,
    ) -> None:
        now = int(time.time())
        expires_at = now + ttl_seconds if ttl_seconds is not None else None
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO resources(kind,id,project_id,created_at,expires_at,size_bytes) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (kind, resource_id, project_id, now, expires_at),
            )

    def reserve_schedule(
        self, resource_id: str, project_id: str, size_bytes: int
    ) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            usage = db.execute(
                "SELECT count(*) count, coalesce(sum(size_bytes),0) bytes "
                "FROM resources WHERE kind='schedule' AND project_id=?",
                (project_id,),
            ).fetchone()
            if (
                int(usage["count"]) + 1 > MAX_SCHEDULES_PER_PROJECT
                or int(usage["bytes"]) + size_bytes
                > MAX_SCHEDULE_BYTES_PER_PROJECT
            ):
                raise ScheduleQuotaError
            db.execute(
                "INSERT INTO resources(kind,id,project_id,created_at,expires_at,size_bytes) "
                "VALUES ('schedule', ?, ?, ?, NULL, ?)",
                (resource_id, project_id, int(time.time()), size_bytes),
            )

    def resize_schedule(
        self, resource_id: str, project_id: str, size_bytes: int
    ) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT size_bytes FROM resources "
                "WHERE kind='schedule' AND id=? AND project_id=?",
                (resource_id, project_id),
            ).fetchone()
            if existing is None:
                raise KeyError(resource_id)
            other_bytes = db.execute(
                "SELECT coalesce(sum(size_bytes),0) FROM resources "
                "WHERE kind='schedule' AND project_id=? AND id<>?",
                (project_id, resource_id),
            ).fetchone()[0]
            if int(other_bytes) + size_bytes > MAX_SCHEDULE_BYTES_PER_PROJECT:
                raise ScheduleQuotaError
            db.execute(
                "UPDATE resources SET size_bytes=? "
                "WHERE kind='schedule' AND id=? AND project_id=?",
                (size_bytes, resource_id, project_id),
            )

    def disown(
        self, kind: str, resource_id: str, project_id: str | None = None
    ) -> bool:
        query = "DELETE FROM resources WHERE kind=? AND id=?"
        values: tuple[object, ...] = (kind, resource_id)
        if project_id is not None:
            query += " AND project_id=?"
            values += (project_id,)
        with self._connect() as db:
            cursor = db.execute(query, values)
        return cursor.rowcount == 1

    def is_owner(self, kind: str, resource_id: str, project_id: str | None) -> bool:
        if project_id is None:
            return False
        with self._connect() as db:
            db.execute(
                "DELETE FROM resources WHERE expires_at IS NOT NULL AND expires_at<=?",
                (int(time.time()),),
            )
            row = db.execute("SELECT project_id FROM resources WHERE kind=? AND id=?", (kind, resource_id)).fetchone()
        return row is not None and secrets.compare_digest(str(row[0]), project_id)

    def owner(self, kind: str, resource_id: str) -> str | None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM resources WHERE expires_at IS NOT NULL AND expires_at<=?",
                (int(time.time()),),
            )
            row = db.execute("SELECT project_id FROM resources WHERE kind=? AND id=?", (kind, resource_id)).fetchone()
        return str(row[0]) if row else None

    def audit(self, project_id: str | None, actor: str, action: str, resource: str | None = None) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO audit_events(project_id, actor, action, resource, created_at) VALUES (?, ?, ?, ?, ?)", (project_id, actor, action, resource, int(time.time())))
            db.execute(
                "DELETE FROM audit_events WHERE id < ("
                "SELECT id FROM audit_events ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (MAX_AUDIT_EVENTS - 1,),
            )

    def audits(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def put_profile(
        self,
        project_id: str,
        profile_id: str,
        state: dict[str, object],
        ttl: int | None,
        *,
        expires_at: int | None = None,
    ) -> None:
        nonce = secrets.token_bytes(12)
        aad = f"{project_id}:{profile_id}".encode()
        payload = nonce + self._cipher.encrypt(nonce, json.dumps(state, separators=(",", ":")).encode(), aad)
        now = int(time.time())
        expires = expires_at if expires_at is not None else now + ttl if ttl else None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM profiles WHERE project_id=? AND expires_at IS NOT NULL AND expires_at<=?",
                (project_id, now),
            )
            self._check_profile_quota(db, project_id, profile_id, payload)
            db.execute("INSERT OR REPLACE INTO profiles VALUES (?, ?, ?, ?, ?)", (profile_id, project_id, payload, now, expires))
            db.execute(
                "INSERT OR IGNORE INTO resources"
                "(kind,id,project_id,created_at,expires_at,size_bytes) "
                "VALUES ('profile', ?, ?, ?, ?, 0)",
                (profile_id, project_id, now, expires),
            )

    @staticmethod
    def _check_profile_quota(db, project_id: str, profile_id: str, payload: bytes) -> None:
        usage = db.execute(
            "SELECT count(*) count, coalesce(sum(length(payload)),0) bytes "
            "FROM profiles WHERE project_id=?",
            (project_id,),
        ).fetchone()
        existing = db.execute(
            "SELECT length(payload) bytes FROM profiles WHERE project_id=? AND id=?",
            (project_id, profile_id),
        ).fetchone()
        count = int(usage["count"]) + (0 if existing else 1)
        total = (
            int(usage["bytes"])
            - (int(existing["bytes"]) if existing else 0)
            + len(payload)
        )
        if count > MAX_PROFILES_PER_PROJECT or total > MAX_PROFILE_BYTES_PER_PROJECT:
            raise ProfileQuotaError

    def get_profile(self, project_id: str, profile_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload, expires_at FROM profiles WHERE id=? AND project_id=?", (profile_id, project_id)).fetchone()
            if row and row["expires_at"] is not None and row["expires_at"] <= int(time.time()):
                db.execute("DELETE FROM profiles WHERE id=? AND project_id=?", (profile_id, project_id))
                db.execute(
                    "DELETE FROM resources WHERE kind='profile' AND id=? AND project_id=?",
                    (profile_id, project_id),
                )
                row = None
        if row is None:
            return None
        payload = bytes(row["payload"])
        clear = self._cipher.decrypt(payload[:12], payload[12:], f"{project_id}:{profile_id}".encode())
        return json.loads(clear)

    def get_profile_any(self, profile_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT project_id FROM profiles WHERE id=?", (profile_id,)).fetchone()
        return self.get_profile(str(row["project_id"]), profile_id) if row else None

    def put_profile_any(self, profile_id: str, state: dict[str, object]) -> bool:
        now = int(time.time())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM profiles WHERE expires_at IS NOT NULL AND expires_at<=?",
                (now,),
            )
            row = db.execute(
                "SELECT project_id, expires_at FROM profiles WHERE id=?",
                (profile_id,),
            ).fetchone()
            if row is None:
                return False
            project_id = str(row["project_id"])
            nonce = secrets.token_bytes(12)
            payload = nonce + self._cipher.encrypt(
                nonce,
                json.dumps(state, separators=(",", ":")).encode(),
                f"{project_id}:{profile_id}".encode(),
            )
            self._check_profile_quota(db, project_id, profile_id, payload)
            updated = db.execute(
                "UPDATE profiles SET payload=?,updated_at=? "
                "WHERE id=? AND project_id=?",
                (payload, now, profile_id, project_id),
            )
            return updated.rowcount == 1

    def delete_profile(self, project_id: str, profile_id: str) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("DELETE FROM profiles WHERE id=? AND project_id=?", (profile_id, project_id))
            if cursor.rowcount == 1:
                db.execute(
                    "DELETE FROM resources WHERE kind='profile' AND id=? AND project_id=?",
                    (profile_id, project_id),
                )
        return cursor.rowcount == 1

    def put_baseline(self, project_id: str, name: str, body: bytes) -> dict[str, object]:
        digest = hashlib.sha256(body).hexdigest()
        updated_at = int(time.time())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            usage = db.execute(
                "SELECT count(*) count, coalesce(sum(length(body)),0) bytes "
                "FROM baselines WHERE project_id=?",
                (project_id,),
            ).fetchone()
            existing = db.execute(
                "SELECT length(body) bytes FROM baselines WHERE project_id=? AND name=?",
                (project_id, name),
            ).fetchone()
            count = int(usage["count"]) + (0 if existing else 1)
            total = int(usage["bytes"]) - (int(existing["bytes"]) if existing else 0) + len(body)
            if count > MAX_BASELINES_PER_PROJECT or total > MAX_BASELINE_BYTES_PER_PROJECT:
                raise BaselineQuotaError
            db.execute("INSERT OR REPLACE INTO baselines VALUES (?, ?, ?, ?, ?)", (name, project_id, body, digest, updated_at))
        return {"name": name, "sha256": digest, "bytes": len(body), "updated_at": updated_at}

    def get_baseline(self, project_id: str, name: str) -> bytes | None:
        with self._connect() as db:
            row = db.execute("SELECT body FROM baselines WHERE name=? AND project_id=?", (name, project_id)).fetchone()
        return bytes(row["body"]) if row else None

    def list_baselines(self, project_id: str) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute("SELECT name, sha256, length(body) bytes, updated_at FROM baselines WHERE project_id=? ORDER BY name", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def delete_baseline(self, project_id: str, name: str) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "DELETE FROM baselines WHERE project_id=? AND name=?",
                (project_id, name),
            )
            if cursor.rowcount == 1:
                db.execute(
                    "DELETE FROM resources WHERE kind='baseline' AND id=? AND project_id=?",
                    (name, project_id),
                )
        return cursor.rowcount == 1
