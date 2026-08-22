from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from codex_sync.cloud.attachments import AttachmentRepository
from codex_sync.cloud.auth import AuthService
from codex_sync.cloud.devices import DeviceService
from codex_sync.cloud.restore import RestoreRepository
from codex_sync.cloud.snapshots import SnapshotRepository
from codex_sync.hashing import sha256_bytes

UTC = timezone.utc


class SnapshotManifestSource(Protocol):
    def list_threads(
        self,
        user_id: str,
        access_token: str,
        codex_thread_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_sessions(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def list_attachments(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...

    def list_references(self, user_id: str, access_token: str) -> list[dict[str, Any]]: ...


class CombinedSnapshotSource:
    def __init__(
        self,
        restore_repository: RestoreRepository,
        attachment_repository: AttachmentRepository,
    ) -> None:
        self.restore_repository = restore_repository
        self.attachment_repository = attachment_repository

    def list_threads(
        self,
        user_id: str,
        access_token: str,
        codex_thread_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.restore_repository.list_threads(
            user_id,
            access_token,
            codex_thread_id,
            workspace_id,
        )

    def list_sessions(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        return self.restore_repository.list_sessions(user_id, access_token)

    def list_attachments(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        return self.attachment_repository.list_attachments(user_id, access_token)

    def list_references(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        return self.attachment_repository.list_references(user_id, access_token)


class SnapshotRestoreRepository:
    def __init__(
        self,
        manifest: dict[str, Any],
        base_repository: RestoreRepository,
    ) -> None:
        self.manifest = manifest
        self.base_repository = base_repository

    def list_threads(
        self,
        user_id: str,
        access_token: str,
        codex_thread_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            item
            for item in self.manifest.get("threads", [])
            if isinstance(item, dict)
        ]
        if codex_thread_id:
            rows = [item for item in rows if item.get("codex_thread_id") == codex_thread_id]
        if workspace_id:
            rows = [item for item in rows if item.get("workspace_id") == workspace_id]
        return rows

    def list_sessions(self, user_id: str, access_token: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.manifest.get("sessions", [])
            if isinstance(item, dict)
        ]

    def download_session(self, storage_path: str, access_token: str) -> bytes:
        allowed = {
            str(item.get("storage_path") or "")
            for item in self.manifest.get("sessions", [])
            if isinstance(item, dict)
        }
        if storage_path not in allowed:
            raise RuntimeError("Snapshot Session Storage path is not in the selected manifest")
        return self.base_repository.download_session(storage_path, access_token)

    def download_session_to_path(
        self,
        user_id: str,
        storage_path: str,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
        access_token: str,
    ) -> None:
        allowed = {
            str(item.get("storage_path") or "")
            for item in self.manifest.get("sessions", [])
            if isinstance(item, dict)
        }
        if storage_path not in allowed:
            raise RuntimeError("Snapshot Session Storage path is not in the selected manifest")
        download_to_path = getattr(
            self.base_repository,
            "download_session_to_path",
            None,
        )
        if callable(download_to_path):
            download_to_path(
                user_id,
                storage_path,
                destination,
                expected_sha256,
                expected_size,
                access_token,
            )
            return
        destination.write_bytes(
            self.base_repository.download_session(storage_path, access_token)
        )


class SnapshotManager:
    def __init__(
        self,
        auth: AuthService,
        devices: DeviceService,
        repository: SnapshotRepository,
        source: SnapshotManifestSource,
        base_restore_repository: RestoreRepository,
    ) -> None:
        self.auth = auth
        self.devices = devices
        self.repository = repository
        self.source = source
        self.base_restore_repository = base_restore_repository

    def create(
        self,
        *,
        snapshot_type: str = "manual",
        label: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        if snapshot_type not in {"automatic", "manual", "pre_restore"}:
            raise ValueError(f"Unsupported Snapshot type: {snapshot_type}")
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before creating a Snapshot")
        device = self.devices.current_device()
        self.devices.register_current_device()
        threads = self.source.list_threads(
            session.user.id,
            session.access_token,
            workspace_id=workspace_id,
        )
        sessions = self.source.list_sessions(session.user.id, session.access_token)
        attachments = self.source.list_attachments(session.user.id, session.access_token)
        references = self.source.list_references(session.user.id, session.access_token)
        manifest = {
            "version": 1,
            "snapshot_type": snapshot_type,
            "workspace_id": workspace_id,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "threads": threads,
            "sessions": sessions,
            "attachments": attachments,
            "references": references,
        }
        content = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = sha256_bytes(content)
        snapshot_id = str(uuid.uuid4())
        storage_path = (
            f"users/{session.user.id}/snapshots/{snapshot_id}/manifest.json"
        )
        self.repository.upsert_snapshot(
            session.user.id,
            {
                "id": snapshot_id,
                "source_device_id": device.id,
                "workspace_id": workspace_id,
                "snapshot_type": snapshot_type,
                "label": label,
                "status": "pending",
                "manifest_hash": digest,
                "manifest_storage_path": storage_path,
                "thread_count": len(threads),
                "session_count": len(sessions),
                "attachment_count": len(attachments),
            },
            session.access_token,
        )
        try:
            self.repository.upload_manifest(storage_path, content, session.access_token)
            self.repository.upsert_snapshot_items(
                session.user.id,
                self._snapshot_items(snapshot_id, threads, sessions, attachments),
                session.access_token,
            )
            return self.repository.update_snapshot(
                session.user.id,
                snapshot_id,
                {
                    "status": "complete",
                    "completed_at": datetime.now(tz=UTC).isoformat(),
                },
                session.access_token,
            )
        except Exception as exc:
            try:
                self.repository.update_snapshot(
                    session.user.id,
                    snapshot_id,
                    {"status": "failed"},
                    session.access_token,
                )
            except Exception:
                pass
            raise RuntimeError(f"Snapshot creation failed: {exc}") from exc

    def list(self) -> list[dict[str, Any]]:
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before listing Snapshots")
        return self.repository.list_snapshots(session.user.id, session.access_token)

    def load_restore_repository(self, snapshot_id: str) -> SnapshotRestoreRepository:
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before restoring a Snapshot")
        snapshots = self.repository.list_snapshots(session.user.id, session.access_token)
        selected = next(
            (item for item in snapshots if str(item.get("id") or "") == snapshot_id),
            None,
        )
        if selected is None:
            raise RuntimeError(f"Snapshot was not found: {snapshot_id}")
        if str(selected.get("status") or "") != "complete":
            raise RuntimeError(f"Snapshot is not complete: {snapshot_id}")
        storage_path = str(selected.get("manifest_storage_path") or "")
        expected_hash = str(selected.get("manifest_hash") or "")
        expected_prefix = f"users/{session.user.id}/snapshots/{snapshot_id}/"
        if not storage_path.startswith(expected_prefix) or not storage_path.endswith(
            "/manifest.json"
        ):
            raise RuntimeError(f"Snapshot has an unsafe Manifest path: {snapshot_id}")
        content = self.repository.download_manifest(storage_path, session.access_token)
        if sha256_bytes(content) != expected_hash:
            raise RuntimeError(f"Snapshot Manifest SHA256 mismatch: {snapshot_id}")
        try:
            manifest = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Snapshot Manifest is invalid: {snapshot_id}") from exc
        if not isinstance(manifest, dict) or manifest.get("version") != 1:
            raise RuntimeError(f"Snapshot Manifest version is unsupported: {snapshot_id}")
        return SnapshotRestoreRepository(manifest, self.base_restore_repository)

    @staticmethod
    def _snapshot_items(
        snapshot_id: str,
        threads: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for object_type, rows in (
            ("thread", threads),
            ("session", sessions),
            ("attachment", attachments),
        ):
            for row in rows:
                object_id = str(row.get("id") or "")
                try:
                    str(uuid.UUID(object_id))
                except ValueError:
                    continue
                items.append(
                    {
                        "snapshot_id": snapshot_id,
                        "object_type": object_type,
                        "object_id": object_id,
                        "content_hash": row.get("content_hash") or row.get("sha256"),
                        "storage_path": row.get("storage_path"),
                    }
                )
        return items
