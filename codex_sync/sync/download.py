from __future__ import annotations

import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from codex_sync.cloud.auth import AuthService
from codex_sync.cloud.devices import DeviceService
from codex_sync.cloud.restore import RestoreRepository
from codex_sync.hashing import sha256_bytes
from codex_sync.local.repair_engine import Paths, ensure_environment
from codex_sync.local.restore_engine import (
    LocalRestoreSummary,
    PreparedTextRestore,
    parse_session_meta,
    restore_text_history,
    safe_restore_path,
)


class WorkspaceResolver:
    def resolve_local_path(self, workspace_id: str) -> Path | None:
        raise NotImplementedError

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ManualDownloadSummary:
    cloud_threads: int
    cloud_sessions: int
    downloaded_session_objects: int
    reused_local_sessions: int
    local_restore: LocalRestoreSummary

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["local_restore"] = self.local_restore.to_dict()
        return payload


def required_text(item: dict[str, Any], field: str, object_name: str) -> str:
    value = str(item.get(field) or "").strip()
    if not value:
        raise RuntimeError(f"Cloud {object_name} is missing {field}")
    return value


def choose_session(
    thread: dict[str, Any],
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    cloud_thread_id = required_text(thread, "id", "Thread")
    candidates = [item for item in sessions if str(item.get("thread_id") or "") == cloud_thread_id]
    if not candidates:
        raise RuntimeError(
            f"Cloud Thread {thread.get('codex_thread_id') or cloud_thread_id} has no Session"
        )
    preferred_path = str(thread.get("rollout_relative_path") or "")
    preferred = [item for item in candidates if str(item.get("relative_path") or "") == preferred_path]
    if len(preferred) == 1:
        return preferred[0]
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(
        f"Cloud Thread {thread.get('codex_thread_id') or cloud_thread_id} has an ambiguous Session set"
    )


class ManualDownloadEngine:
    def __init__(
        self,
        paths: Paths,
        auth: AuthService,
        devices: DeviceService,
        repository: RestoreRepository,
        workspaces: WorkspaceResolver | None = None,
    ) -> None:
        self.paths = paths
        self.auth = auth
        self.devices = devices
        self.repository = repository
        self.workspaces = workspaces

    def restore(
        self,
        *,
        codex_thread_id: str | None = None,
        workspace_id: str | None = None,
        target_cwd: Path | None = None,
    ) -> ManualDownloadSummary:
        ensure_environment(self.paths)
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before restoring history")
        override_cwd = (
            target_cwd.expanduser().resolve(strict=False) if target_cwd else None
        )
        if override_cwd is not None and not override_cwd.is_dir():
            raise RuntimeError(f"Target working directory does not exist: {override_cwd}")

        self.devices.register_current_device()
        if workspace_id:
            threads = self.repository.list_threads(
                session.user.id,
                session.access_token,
                codex_thread_id,
                workspace_id,
            )
        else:
            threads = self.repository.list_threads(
                session.user.id,
                session.access_token,
                codex_thread_id,
            )
        if not threads and codex_thread_id:
            raise RuntimeError(f"Cloud Thread was not found: {codex_thread_id}")
        if not threads and workspace_id:
            raise RuntimeError(f"Cloud Workspace has no restorable Threads: {workspace_id}")
        sessions = self.repository.list_sessions(session.user.id, session.access_token)
        target_cwds: dict[str, Path] = {}
        for thread in threads:
            local_thread_id = required_text(thread, "codex_thread_id", "Thread")
            cloud_workspace_id = str(thread.get("workspace_id") or "").strip()
            chosen_cwd = override_cwd
            if chosen_cwd is None and cloud_workspace_id:
                chosen_cwd = (
                    self.workspaces.resolve_local_path(cloud_workspace_id)
                    if self.workspaces
                    else None
                )
                if chosen_cwd is None:
                    raise RuntimeError(
                        f"Workspace {cloud_workspace_id} is not mapped on this device"
                    )
                chosen_cwd = chosen_cwd.expanduser().resolve(strict=False)
            if chosen_cwd is None:
                chosen_cwd = Path.home().resolve(strict=False)
            if not chosen_cwd.is_dir():
                raise RuntimeError(f"Target working directory does not exist: {chosen_cwd}")
            target_cwds[local_thread_id] = chosen_cwd

        prepared: list[PreparedTextRestore] = []
        downloaded_objects = 0
        reused_local = 0
        staged_by_hash: dict[str, bytes] = {}

        with tempfile.TemporaryDirectory(prefix="codex-sync-restore-") as temp_dir:
            staging_root = Path(temp_dir)
            for thread in threads:
                cloud_thread_id = required_text(thread, "id", "Thread")
                local_thread_id = required_text(thread, "codex_thread_id", "Thread")
                cloud_session = choose_session(thread, sessions)
                relative_path = required_text(cloud_session, "relative_path", "Session")
                content_hash = required_text(cloud_session, "content_hash", "Session")
                storage_path = required_text(cloud_session, "storage_path", "Session")
                if not SHA256_PATTERN.fullmatch(content_hash):
                    raise RuntimeError(f"Cloud Session has invalid SHA256: {local_thread_id}")
                expected_storage_path = (
                    f"users/{session.user.id}/sessions/{content_hash}.jsonl"
                )
                if storage_path != expected_storage_path:
                    raise RuntimeError(f"Cloud Session has an unsafe Storage path: {local_thread_id}")
                try:
                    expected_size = int(cloud_session.get("file_size"))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(f"Cloud Session has invalid file_size: {local_thread_id}") from exc
                if expected_size < 0:
                    raise RuntimeError(f"Cloud Session has invalid file_size: {local_thread_id}")

                target = safe_restore_path(self.paths.codex_home, relative_path)
                content: bytes | None = None
                if target.exists():
                    reused_local += 1
                else:
                    content = staged_by_hash.get(content_hash)
                    if content is None:
                        content = self.repository.download_session(
                            storage_path,
                            session.access_token,
                        )
                        if len(content) != expected_size:
                            raise RuntimeError(
                                f"Downloaded Session size mismatch: {local_thread_id}"
                            )
                        actual_hash = sha256_bytes(content)
                        if actual_hash != content_hash:
                            raise RuntimeError(
                                f"Downloaded Session SHA256 mismatch: {local_thread_id}"
                            )
                        parse_session_meta(content, local_thread_id)
                        staged_path = staging_root / f"{content_hash}.jsonl"
                        staged_path.write_bytes(content)
                        if sha256_bytes(staged_path.read_bytes()) != content_hash:
                            raise RuntimeError(
                                f"Staged Session SHA256 mismatch: {local_thread_id}"
                            )
                        staged_by_hash[content_hash] = content
                        downloaded_objects += 1

                prepared.append(
                    PreparedTextRestore(
                        cloud_thread_id=cloud_thread_id,
                        codex_thread_id=local_thread_id,
                        codex_session_id=(
                            str(cloud_session["codex_session_id"])
                            if cloud_session.get("codex_session_id")
                            else None
                        ),
                        relative_path=relative_path,
                        cloud_content_hash=content_hash,
                        file_size=expected_size,
                        content=content,
                        title=str(thread.get("title") or ""),
                        source=str(thread.get("source") or ""),
                        model_provider=str(thread.get("model_provider") or ""),
                        model=str(thread["model"]) if thread.get("model") else None,
                        archived=bool(thread.get("archived")),
                        codex_created_at=(
                            str(thread["codex_created_at"])
                            if thread.get("codex_created_at")
                            else None
                        ),
                        codex_updated_at=(
                            str(thread["codex_updated_at"])
                            if thread.get("codex_updated_at")
                            else None
                        ),
                        target_cwd=target_cwds[local_thread_id],
                    )
                )

            local_summary = restore_text_history(self.paths, prepared)

        return ManualDownloadSummary(
            cloud_threads=len(threads),
            cloud_sessions=len(prepared),
            downloaded_session_objects=downloaded_objects,
            reused_local_sessions=reused_local,
            local_restore=local_summary,
        )
