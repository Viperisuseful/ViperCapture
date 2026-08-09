"""Small self-hosted control plane: projects, keys, ownership, profiles, and metrics."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_BASELINES_PER_PROJECT = 100
MAX_BASELINE_BYTES_PER_PROJECT = 512 * 1024 * 1024
MAX_PROFILES_PER_PROJECT = 100
MAX_PROFILE_BYTES_PER_PROJECT = 512 * 1024 * 1024
LIMIT_LEASE_SECONDS = 15 * 60


class BaselineQuotaError(RuntimeError):
    pass


class ProfileQuotaError(RuntimeError):
    pass


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

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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

    @contextmanager
    def _connect(self):
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
        return [dict(row) for row in rows]

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
        return hashlib.blake2b(
            raw.encode(), key=self._key_hash_secret, digest_size=32
        ).digest()

    async def acquire(self, identity: dict[str, object]) -> tuple[bool, str | None]:
        project_id = str(identity["project_id"])
        async with self._limit_lock:
            now = time.time()
            lease_id = secrets.token_hex(16)
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM rate_events WHERE created_at<=?", (now - 60,))
                db.execute("DELETE FROM active_leases WHERE expires_at<=?", (now,))
                rpm = db.execute(
                    "SELECT count(*) FROM rate_events WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
                if int(rpm) >= int(identity["rpm"]):
                    return False, "rate_limit_exceeded"
                active = db.execute(
                    "SELECT count(*) FROM active_leases WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
                if int(active) >= int(identity["concurrency"]):
                    return False, "concurrency_limit_exceeded"
                db.execute(
                    "INSERT INTO rate_events VALUES (?, ?, ?)",
                    (secrets.token_hex(16), project_id, now),
                )
                db.execute(
                    "INSERT INTO active_leases VALUES (?, ?, ?)",
                    (lease_id, project_id, now + LIMIT_LEASE_SECONDS),
                )
            self._leases[project_id].append(lease_id)
        return True, None

    async def release(self, project_id: str) -> None:
        async with self._limit_lock:
            if not self._leases[project_id]:
                return
            lease_id = self._leases[project_id].popleft()
            with self._connect() as db:
                db.execute("DELETE FROM active_leases WHERE id=?", (lease_id,))

    async def acquire_worker(self, project_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT concurrency FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        if row is None:
            return False
        async with self._limit_lock:
            now = time.time()
            lease_id = secrets.token_hex(16)
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM active_leases WHERE expires_at<=?", (now,))
                active = db.execute(
                    "SELECT count(*) FROM active_leases WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
                if int(active) >= int(row["concurrency"]):
                    return False
                db.execute(
                    "INSERT INTO active_leases VALUES (?, ?, ?)",
                    (lease_id, project_id, now + LIMIT_LEASE_SECONDS),
                )
            self._leases[project_id].append(lease_id)
        return True

    def own(self, kind: str, resource_id: str, project_id: str) -> None:
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO resources VALUES (?, ?, ?)", (kind, resource_id, project_id))

    def is_owner(self, kind: str, resource_id: str, project_id: str | None) -> bool:
        if project_id is None:
            return False
        with self._connect() as db:
            row = db.execute("SELECT project_id FROM resources WHERE kind=? AND id=?", (kind, resource_id)).fetchone()
        return row is not None and secrets.compare_digest(str(row[0]), project_id)

    def owner(self, kind: str, resource_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT project_id FROM resources WHERE kind=? AND id=?", (kind, resource_id)).fetchone()
        return str(row[0]) if row else None

    def audit(self, project_id: str | None, actor: str, action: str, resource: str | None = None) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO audit_events(project_id, actor, action, resource, created_at) VALUES (?, ?, ?, ?, ?)", (project_id, actor, action, resource, int(time.time())))

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
            db.execute("INSERT OR REPLACE INTO profiles VALUES (?, ?, ?, ?, ?)", (profile_id, project_id, payload, now, expires))

    def get_profile(self, project_id: str, profile_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload, expires_at FROM profiles WHERE id=? AND project_id=?", (profile_id, project_id)).fetchone()
            if row and row["expires_at"] is not None and row["expires_at"] <= int(time.time()):
                db.execute("DELETE FROM profiles WHERE id=? AND project_id=?", (profile_id, project_id))
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
        with self._connect() as db:
            row = db.execute("SELECT project_id, expires_at FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if row is None:
            return False
        expires_at = int(row["expires_at"]) if row["expires_at"] else None
        if expires_at is not None and expires_at <= int(time.time()):
            self.delete_profile(str(row["project_id"]), profile_id)
            return False
        self.put_profile(
            str(row["project_id"]),
            profile_id,
            state,
            None,
            expires_at=expires_at,
        )
        return True

    def delete_profile(self, project_id: str, profile_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM profiles WHERE id=? AND project_id=?", (profile_id, project_id))
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
            cursor = db.execute(
                "DELETE FROM baselines WHERE project_id=? AND name=?",
                (project_id, name),
            )
        return cursor.rowcount == 1
