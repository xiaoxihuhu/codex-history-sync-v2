from __future__ import annotations

import json
import platform
import socket
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from codex_sync.config import AppPaths, default_app_paths
from codex_sync.models import AuthSession, DeviceIdentity, utc_now
from codex_sync.security import SecretProtector, default_secret_protector

SCHEMA_VERSION = 4


@dataclass(frozen=True)
class WorkspaceMapping:
    workspace_id: str
    workspace_name: str
    local_path: str
    updated_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "local_path": self.local_path,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class UploadQueueItem:
    id: int
    object_kind: str
    object_key: str
    content_hash: str
    size: int
    mtime_ns: int
    attempt_count: int
    status: str
    next_attempt_at: str
    last_error: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "object_kind": self.object_kind,
            "object_key": self.object_key,
            "content_hash": self.content_hash,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "attempt_count": self.attempt_count,
            "status": self.status,
            "next_attempt_at": self.next_attempt_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SyncStateStore:
    def __init__(
        self,
        paths: AppPaths | None = None,
        protector: SecretProtector | None = None,
    ) -> None:
        self.paths = paths or default_app_paths()
        self.paths.ensure()
        self.protector = protector or default_secret_protector()
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.paths.state_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS auth_session (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    protected_payload BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_identity (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    device_id TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    os_name TEXT NOT NULL,
                    os_version TEXT NOT NULL,
                    client_version TEXT NOT NULL,
                    first_registered_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    last_backup_at TEXT
                );
                CREATE TABLE IF NOT EXISTS workspace_mappings (
                    workspace_id TEXT PRIMARY KEY,
                    workspace_name TEXT NOT NULL DEFAULT '',
                    local_path TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_objects (
                    object_kind TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK (size >= 0),
                    mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
                    sha256 TEXT NOT NULL,
                    last_uploaded_hash TEXT,
                    last_uploaded_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (object_kind, object_key)
                );
                CREATE TABLE IF NOT EXISTS upload_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_kind TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK (size >= 0),
                    mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'completed')),
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (object_kind, object_key)
                );
                CREATE INDEX IF NOT EXISTS upload_queue_pending_idx
                    ON upload_queue(status, next_attempt_at, updated_at);
                """
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

    def save_auth_session(self, session: AuthSession) -> None:
        serialized = json.dumps(session.to_storage_dict(), separators=(",", ":")).encode("utf-8")
        protected = self.protector.protect(serialized)
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_session (singleton, protected_payload, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    protected_payload = excluded.protected_payload,
                    updated_at = excluded.updated_at
                """,
                (protected, now),
            )
            conn.commit()

    def load_auth_session(self) -> AuthSession | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT protected_payload FROM auth_session WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        plaintext = self.protector.unprotect(bytes(row["protected_payload"]))
        payload = json.loads(plaintext.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Stored Auth session payload is invalid")
        return AuthSession.from_storage_dict(payload)

    def clear_auth_session(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM auth_session WHERE singleton = 1")
            conn.commit()

    def get_or_create_device(self, client_version: str) -> DeviceIdentity:
        existing = self.get_device()
        if existing:
            return existing

        now = utc_now().isoformat()
        device = DeviceIdentity(
            id=str(uuid.uuid4()),
            device_name=socket.gethostname() or "Windows device",
            os_name=platform.system() or "Windows",
            os_version=platform.version() or platform.release(),
            client_version=client_version,
            first_registered_at=now,
            last_seen_at=now,
            last_backup_at=None,
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO device_identity (
                    singleton, device_id, device_name, os_name, os_version,
                    client_version, first_registered_at, last_seen_at, last_backup_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device.id,
                    device.device_name,
                    device.os_name,
                    device.os_version,
                    device.client_version,
                    device.first_registered_at,
                    device.last_seen_at,
                    device.last_backup_at,
                ),
            )
            conn.commit()
        return self.get_device() or device

    def get_device(self) -> DeviceIdentity | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT device_id, device_name, os_name, os_version, client_version,
                       first_registered_at, last_seen_at, last_backup_at
                FROM device_identity WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            return None
        return DeviceIdentity(
            id=str(row["device_id"]),
            device_name=str(row["device_name"]),
            os_name=str(row["os_name"]),
            os_version=str(row["os_version"]),
            client_version=str(row["client_version"]),
            first_registered_at=str(row["first_registered_at"]),
            last_seen_at=str(row["last_seen_at"]) if row["last_seen_at"] else None,
            last_backup_at=str(row["last_backup_at"]) if row["last_backup_at"] else None,
        )

    def mark_device_seen(self) -> DeviceIdentity:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                "UPDATE device_identity SET last_seen_at = ? WHERE singleton = 1",
                (now,),
            )
            conn.commit()
        device = self.get_device()
        if device is None:
            raise RuntimeError("Device identity has not been initialized")
        return device

    def mark_device_backed_up(self) -> DeviceIdentity:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE device_identity
                SET last_seen_at = ?, last_backup_at = ?
                WHERE singleton = 1
                """,
                (now, now),
            )
            conn.commit()
        device = self.get_device()
        if device is None:
            raise RuntimeError("Device identity has not been initialized")
        return device

    def save_workspace_mapping(
        self,
        workspace_id: str,
        local_path: Path,
        workspace_name: str = "",
    ) -> WorkspaceMapping:
        normalized_id = str(uuid.UUID(workspace_id))
        target = local_path.expanduser().resolve(strict=False)
        if not target.is_dir():
            raise RuntimeError(f"Workspace directory does not exist: {target}")
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workspace_mappings (
                    workspace_id, workspace_name, local_path, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    workspace_name = excluded.workspace_name,
                    local_path = excluded.local_path,
                    updated_at = excluded.updated_at
                """,
                (normalized_id, workspace_name.strip(), str(target), now),
            )
            conn.commit()
        mapping = self.get_workspace_mapping(normalized_id)
        if mapping is None:
            raise RuntimeError("Workspace mapping was not persisted")
        return mapping

    def get_workspace_mapping(self, workspace_id: str) -> WorkspaceMapping | None:
        normalized_id = str(uuid.UUID(workspace_id))
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT workspace_id, workspace_name, local_path, updated_at
                FROM workspace_mappings
                WHERE workspace_id = ?
                """,
                (normalized_id,),
            ).fetchone()
        return self._workspace_mapping_from_row(row) if row else None

    def list_workspace_mappings(self) -> list[WorkspaceMapping]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT workspace_id, workspace_name, local_path, updated_at
                FROM workspace_mappings
                ORDER BY workspace_name COLLATE NOCASE, local_path COLLATE NOCASE
                """
            ).fetchall()
        return [self._workspace_mapping_from_row(row) for row in rows]

    @staticmethod
    def _workspace_mapping_from_row(row: sqlite3.Row) -> WorkspaceMapping:
        return WorkspaceMapping(
            workspace_id=str(row["workspace_id"]),
            workspace_name=str(row["workspace_name"]),
            local_path=str(row["local_path"]),
            updated_at=str(row["updated_at"]),
        )

    def get_sync_object(
        self,
        object_kind: str,
        object_key: str,
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT object_kind, object_key, size, mtime_ns, sha256,
                       last_uploaded_hash, last_uploaded_at, updated_at
                FROM sync_objects
                WHERE object_kind = ? AND object_key = ?
                """,
                (object_kind, object_key),
            ).fetchone()

    def record_sync_object(
        self,
        *,
        object_kind: str,
        object_key: str,
        size: int,
        mtime_ns: int,
        sha256: str,
        last_uploaded_hash: str | None = None,
        last_uploaded_at: str | None = None,
    ) -> None:
        if size < 0 or mtime_ns < 0:
            raise ValueError("Sync object size and mtime must be non-negative")
        now = utc_now().isoformat()
        with self.connect() as conn:
            previous = conn.execute(
                """
                SELECT last_uploaded_hash, last_uploaded_at
                FROM sync_objects
                WHERE object_kind = ? AND object_key = ?
                """,
                (object_kind, object_key),
            ).fetchone()
            uploaded_hash = (
                last_uploaded_hash
                if last_uploaded_hash is not None
                else previous["last_uploaded_hash"]
                if previous
                else None
            )
            uploaded_at = (
                last_uploaded_at
                if last_uploaded_at is not None
                else previous["last_uploaded_at"]
                if previous
                else None
            )
            conn.execute(
                """
                INSERT INTO sync_objects (
                    object_kind, object_key, size, mtime_ns, sha256,
                    last_uploaded_hash, last_uploaded_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(object_kind, object_key) DO UPDATE SET
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    sha256 = excluded.sha256,
                    last_uploaded_hash = excluded.last_uploaded_hash,
                    last_uploaded_at = excluded.last_uploaded_at,
                    updated_at = excluded.updated_at
                """,
                (
                    object_kind,
                    object_key,
                    size,
                    mtime_ns,
                    sha256,
                    uploaded_hash,
                    uploaded_at,
                    now,
                ),
            )
            conn.commit()

    def enqueue_upload(
        self,
        *,
        object_kind: str,
        object_key: str,
        content_hash: str,
        size: int,
        mtime_ns: int,
    ) -> UploadQueueItem:
        if size < 0 or mtime_ns < 0:
            raise ValueError("Upload queue size and mtime must be non-negative")
        now = utc_now().isoformat()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT id, content_hash, status, attempt_count, next_attempt_at,
                       last_error, created_at
                FROM upload_queue
                WHERE object_kind = ? AND object_key = ?
                """,
                (object_kind, object_key),
            ).fetchone()
            if existing and str(existing["content_hash"]) == content_hash:
                status = str(existing["status"])
                attempt_count = int(existing["attempt_count"])
                next_attempt_at = str(existing["next_attempt_at"])
                last_error = existing["last_error"]
                created_at = str(existing["created_at"])
            else:
                status = "pending"
                attempt_count = 0
                next_attempt_at = now
                last_error = None
                created_at = now
            conn.execute(
                """
                INSERT INTO upload_queue (
                    object_kind, object_key, content_hash, size, mtime_ns,
                    attempt_count, status, next_attempt_at, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(object_kind, object_key) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    attempt_count = excluded.attempt_count,
                    status = excluded.status,
                    next_attempt_at = excluded.next_attempt_at,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    object_kind,
                    object_key,
                    content_hash,
                    size,
                    mtime_ns,
                    attempt_count,
                    status,
                    next_attempt_at,
                    last_error,
                    created_at,
                    now,
                ),
            )
            conn.commit()
        item = self.get_upload_queue_item(object_kind, object_key)
        if item is None:
            raise RuntimeError("Upload queue item was not persisted")
        return item

    def mark_upload_completed(
        self,
        *,
        object_kind: str,
        object_key: str,
        content_hash: str,
    ) -> None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE upload_queue
                SET content_hash = ?, status = 'completed', next_attempt_at = ?,
                    last_error = NULL, updated_at = ?
                WHERE object_kind = ? AND object_key = ?
                """,
                (content_hash, now, now, object_kind, object_key),
            )
            conn.commit()

    def mark_upload_failed(
        self,
        *,
        object_kind: str,
        object_key: str,
        error: str,
    ) -> UploadQueueItem | None:
        now_dt = utc_now()
        now = now_dt.isoformat()
        safe_error = error.replace("\r", " ").replace("\n", " ")[:2000]
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT attempt_count FROM upload_queue
                WHERE object_kind = ? AND object_key = ?
                """,
                (object_kind, object_key),
            ).fetchone()
            if row is None:
                return None
            attempt_count = int(row["attempt_count"]) + 1
            delay_seconds = min(300, 2 ** min(attempt_count - 1, 8))
            next_attempt = (now_dt + timedelta(seconds=delay_seconds)).isoformat()
            conn.execute(
                """
                UPDATE upload_queue
                SET attempt_count = ?, status = 'pending', next_attempt_at = ?,
                    last_error = ?, updated_at = ?
                WHERE object_kind = ? AND object_key = ?
                """,
                (
                    attempt_count,
                    next_attempt,
                    safe_error,
                    now,
                    object_kind,
                    object_key,
                ),
            )
            conn.commit()
        return self.get_upload_queue_item(object_kind, object_key)

    def get_upload_queue_item(
        self,
        object_kind: str,
        object_key: str,
    ) -> UploadQueueItem | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, object_kind, object_key, content_hash, size, mtime_ns,
                       attempt_count, status, next_attempt_at, last_error,
                       created_at, updated_at
                FROM upload_queue
                WHERE object_kind = ? AND object_key = ?
                """,
                (object_kind, object_key),
            ).fetchone()
        return self._upload_queue_from_row(row) if row else None

    def list_upload_queue(self, status: str | None = None) -> list[UploadQueueItem]:
        query = """
            SELECT id, object_kind, object_key, content_hash, size, mtime_ns,
                   attempt_count, status, next_attempt_at, last_error,
                   created_at, updated_at
            FROM upload_queue
        """
        params: tuple[object, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY updated_at DESC, id DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._upload_queue_from_row(row) for row in rows]

    @staticmethod
    def _upload_queue_from_row(row: sqlite3.Row) -> UploadQueueItem:
        return UploadQueueItem(
            id=int(row["id"]),
            object_kind=str(row["object_kind"]),
            object_key=str(row["object_key"]),
            content_hash=str(row["content_hash"]),
            size=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
            attempt_count=int(row["attempt_count"]),
            status=str(row["status"]),
            next_attempt_at=str(row["next_attempt_at"]),
            last_error=str(row["last_error"]) if row["last_error"] else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
