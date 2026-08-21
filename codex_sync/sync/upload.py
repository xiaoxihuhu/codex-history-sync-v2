from __future__ import annotations

from dataclasses import asdict, dataclass

from codex_sync.cloud.auth import AuthService
from codex_sync.cloud.backup import ManualUploadRepository
from codex_sync.cloud.devices import DeviceService
from codex_sync.local.catalog import read_stable_file, scan_local_catalog
from codex_sync.local.repair_engine import Paths
from codex_sync.models import utc_now


@dataclass(frozen=True)
class ManualUploadSummary:
    scanned_threads: int
    uploaded_threads: int
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
    ) -> None:
        self.paths = paths
        self.auth = auth
        self.devices = devices
        self.repository = repository

    def upload(self) -> ManualUploadSummary:
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before uploading history")
        local_threads = scan_local_catalog(self.paths)
        device = self.devices.current_device()
        self.devices.register_current_device()
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

        for thread in local_threads:
            cloud_thread_id = thread_ids[thread.codex_thread_id]
            stable = read_stable_file(thread.session.path)
            key = (cloud_thread_id, thread.session.relative_path)
            expected[key] = stable.sha256
            if remote_by_key.get(key) == stable.sha256:
                unchanged += 1
                continue

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
        mismatches = [key for key, digest in expected.items() if verified.get(key) != digest]
        if mismatches:
            raise RuntimeError(f"Cloud verification failed for {len(mismatches)} Session objects")
        self.devices.record_successful_backup()

        return ManualUploadSummary(
            scanned_threads=len(local_threads),
            uploaded_threads=len(thread_ids),
            scanned_sessions=len(local_threads),
            uploaded_session_objects=uploaded_objects,
            upserted_sessions=len(changed_rows),
            unchanged_sessions=unchanged,
            verified_sessions=len(expected),
        )
