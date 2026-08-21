from __future__ import annotations

import json
import platform
import socket
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from codex_sync.config import AppPaths, default_app_paths
from codex_sync.models import AuthSession, DeviceIdentity, utc_now
from codex_sync.security import SecretProtector, default_secret_protector

SCHEMA_VERSION = 1


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
