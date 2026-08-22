from __future__ import annotations

from dataclasses import asdict, dataclass

from codex_sync.cloud.auth import AuthService
from codex_sync.cloud.backup import ManualUploadRepository
from codex_sync.cloud.devices import DeviceService
from codex_sync.local.catalog import scan_local_catalog
from codex_sync.local.repair_engine import Paths
from codex_sync.models import utc_now
from codex_sync.progress import emit_progress
from codex_sync.sync.manifest import build_session_manifest
from codex_sync.sync.state import SyncStateStore
from codex_sync.workspace import WorkspaceManager


@dataclass(frozen=True)
class ManualUploadSummary:
    scanned_threads: int
    uploaded_threads: int
    uploaded_workspaces: int
    scanned_sessions: int
    uploaded_session_objects: int
    upserted_sessions: int
    unchanged_sessions: int
    verified_sessions: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class ManualUploadEngine:
    def __init__(
        self,
        paths: Paths,
        auth: AuthService,
        devices: DeviceService,
        repository: ManualUploadRepository,
        workspaces: WorkspaceManager | None = None,
        state: SyncStateStore | None = None,
    ) -> None:
        self.paths = paths
        self.auth = auth
        self.devices = devices
        self.repository = repository
        self.workspaces = workspaces
        self.state = state or (workspaces.state if workspaces else None)

    def upload(self) -> ManualUploadSummary:
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before uploading history")
        local_threads = scan_local_catalog(self.paths)
        emit_progress(
            "scan",
            object_kind="Session",
            threads=len(local_threads),
            sessions=len(local_threads),
        )
        device = self.devices.current_device()
        self.devices.register_current_device()
        workspace_ids = (
            self.workspaces.prepare_upload(
                local_threads,
                user_id=session.user.id,
                device_id=device.id,
                access_token=session.access_token,
            )
            if self.workspaces
            else {}
        )
        if self.workspaces:
            thread_ids = self.repository.upsert_threads(
                session.user.id,
                device.id,
                local_threads,
                session.access_token,
                workspace_ids,
            )
        else:
            thread_ids = self.repository.upsert_threads(
                session.user.id,
                device.id,
                local_threads,
                session.access_token,
            )
        remote_before = self.repository.list_sessions(session.user.id, session.access_token)
        remote_by_key = {
            (str(item.get("thread_id")), str(item.get("relative_path"))): str(
                item.get("content_hash") or ""
            )
            for item in remote_before
        }
        available_hashes = {
            str(item.get("content_hash"))
            for item in remote_before
            if item.get("content_hash")
        }

        changed_rows: list[dict[str, object]] = []
        uploaded_hashes: set[str] = set()
        unchanged = 0
        uploaded_objects = 0
        expected: dict[tuple[str, str], str] = {}

        manifest = build_session_manifest(local_threads)
        threads_by_key = {
            thread.session.relative_path: thread for thread in local_threads
        }
        queued_entries: list[tuple[str, str, str]] = []
        try:
            for entry in manifest:
                thread = threads_by_key[entry.relative_path]
                cloud_thread_id = thread_ids[thread.codex_thread_id]
                stable = entry.stable_file
                key = (cloud_thread_id, thread.session.relative_path)
                expected[key] = stable.sha256
                if remote_by_key.get(key) == stable.sha256:
                    unchanged += 1
                    if self.state:
                        self.state.enqueue_upload(
                            object_kind=entry.object_kind,
                            object_key=entry.object_key,
                            content_hash=entry.sha256,
                            size=entry.size,
                            mtime_ns=entry.mtime_ns,
                        )
                    continue

                if self.state:
                    self.state.enqueue_upload(
                        object_kind=entry.object_kind,
                        object_key=entry.object_key,
                        content_hash=entry.sha256,
                        size=entry.size,
                        mtime_ns=entry.mtime_ns,
                    )
                    queued_entries.append(
                        (entry.object_kind, entry.object_key, entry.sha256)
                    )
                object_path = f"users/{session.user.id}/sessions/{stable.sha256}.jsonl"
                if stable.sha256 not in available_hashes and stable.sha256 not in uploaded_hashes:
                    object_path = self.repository.upload_session_object(
                        session.user.id,
                        stable,
                        session.access_token,
                    )
                    uploaded_hashes.add(stable.sha256)
                    uploaded_objects += 1
                changed_rows.append(
                    {
                        "thread_id": cloud_thread_id,
                        "codex_session_id": thread.session.codex_session_id,
                        "relative_path": thread.session.relative_path,
                        "content_hash": stable.sha256,
                        "file_size": stable.file_size,
                        "storage_path": object_path,
                        "source_mtime_ns": stable.mtime_ns,
                        "codex_created_at": thread.codex_created_at,
                        "codex_updated_at": thread.codex_updated_at,
                        "last_uploaded_at": utc_now().isoformat(),
                    }
                )

            self.repository.upsert_sessions(
                session.user.id,
                device.id,
                changed_rows,
                session.access_token,
            )
            remote_after = self.repository.list_sessions(session.user.id, session.access_token)
            verified = {
                (str(item.get("thread_id")), str(item.get("relative_path"))): str(
                    item.get("content_hash") or ""
                )
                for item in remote_after
            }
            mismatches = [
                key for key, digest in expected.items() if verified.get(key) != digest
            ]
            if mismatches:
                raise RuntimeError(
                    f"Cloud verification failed for {len(mismatches)} Session objects"
                )
        except Exception as exc:
            if self.state:
                for object_kind, object_key, _ in queued_entries:
                    self.state.mark_upload_failed(
                        object_kind=object_kind,
                        object_key=object_key,
                        error=str(exc),
                    )
            raise
        if self.state:
            for entry in manifest:
                self.state.mark_upload_completed(
                    object_kind=entry.object_kind,
                    object_key=entry.object_key,
                    content_hash=entry.sha256,
                )
        if self.state:
            uploaded_at = utc_now().isoformat()
            for entry in manifest:
                self.state.record_sync_object(
                    object_kind=entry.object_kind,
                    object_key=entry.object_key,
                    size=entry.size,
                    mtime_ns=entry.mtime_ns,
                    sha256=entry.sha256,
                    last_uploaded_hash=entry.sha256,
                    last_uploaded_at=uploaded_at,
                )
        self.devices.record_successful_backup()

        return ManualUploadSummary(
            scanned_threads=len(local_threads),
            uploaded_threads=len(thread_ids),
            uploaded_workspaces=len(set(workspace_ids.values())),
            scanned_sessions=len(local_threads),
            uploaded_session_objects=uploaded_objects,
            upserted_sessions=len(changed_rows),
            unchanged_sessions=unchanged,
            verified_sessions=len(expected),
        )
