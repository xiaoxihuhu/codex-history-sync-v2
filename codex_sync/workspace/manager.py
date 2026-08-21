from __future__ import annotations

import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from codex_sync.cloud.auth import AuthService
from codex_sync.cloud.devices import DeviceService
from codex_sync.cloud.workspaces import WorkspaceRepository
from codex_sync.local.catalog import LocalThreadRecord
from codex_sync.sync.state import SyncStateStore

WORKSPACE_NAMESPACE = uuid.UUID("df206398-8212-4aa5-a741-d74f7e2a9af5")
WINDOWS_PATH_PATTERN = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


@dataclass(frozen=True)
class LogicalWorkspace:
    id: str
    name: str
    local_path: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def normalized_workspace_path(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("Workspace path is empty")
    if WINDOWS_PATH_PATTERN.match(raw):
        return PureWindowsPath(raw).as_posix().casefold()
    resolved = str(Path(raw).expanduser().resolve(strict=False))
    return os.path.normcase(resolved)


def workspace_id_for_path(user_id: str, local_path: str) -> str:
    normalized = normalized_workspace_path(local_path)
    return str(uuid.uuid5(WORKSPACE_NAMESPACE, f"{user_id}\0{normalized}"))


def workspace_name_for_path(local_path: str) -> str:
    raw = local_path.strip()
    if WINDOWS_PATH_PATTERN.match(raw):
        path = PureWindowsPath(raw)
        return path.name or path.drive or "Workspace"
    path = Path(raw)
    return path.name or path.anchor or "Workspace"


class WorkspaceManager:
    def __init__(
        self,
        auth: AuthService,
        devices: DeviceService,
        state: SyncStateStore,
        repository: WorkspaceRepository,
    ) -> None:
        self.auth = auth
        self.devices = devices
        self.state = state
        self.repository = repository

    def prepare_upload(
        self,
        threads: list[LocalThreadRecord],
        *,
        user_id: str,
        device_id: str,
        access_token: str,
    ) -> dict[str, str]:
        workspaces: dict[str, LogicalWorkspace] = {}
        thread_workspace_ids: dict[str, str] = {}
        for thread in threads:
            if not thread.original_cwd:
                continue
            workspace_id = workspace_id_for_path(user_id, thread.original_cwd)
            workspace = LogicalWorkspace(
                id=workspace_id,
                name=workspace_name_for_path(thread.original_cwd),
                local_path=thread.original_cwd,
            )
            workspaces[workspace_id] = workspace
            thread_workspace_ids[thread.codex_thread_id] = workspace_id

        ordered = sorted(workspaces.values(), key=lambda item: item.id)
        self.repository.upsert_workspaces(
            user_id,
            [{"id": item.id, "name": item.name} for item in ordered],
            access_token,
        )
        self.repository.upsert_device_workspaces(
            user_id,
            device_id,
            [
                {"workspace_id": item.id, "local_path": item.local_path}
                for item in ordered
            ],
            access_token,
        )
        for item in ordered:
            local_path = Path(item.local_path).expanduser().resolve(strict=False)
            if local_path.is_dir():
                self.state.save_workspace_mapping(item.id, local_path, item.name)
        return thread_workspace_ids

    def resolve_local_path(self, workspace_id: str) -> Path | None:
        mapping = self.state.get_workspace_mapping(workspace_id)
        if mapping is None:
            return None
        return Path(mapping.local_path)

    def map_workspace(self, workspace_id: str, local_path: Path) -> dict[str, object]:
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before mapping a Workspace")
        target = local_path.expanduser().resolve(strict=False)
        if not target.is_dir():
            raise RuntimeError(f"Workspace directory does not exist: {target}")

        device = self.devices.current_device()
        self.devices.register_current_device()
        cloud_workspaces = self.repository.list_workspaces(
            session.user.id,
            session.access_token,
        )
        selected = next(
            (item for item in cloud_workspaces if str(item.get("id") or "") == workspace_id),
            None,
        )
        if selected is None:
            raise RuntimeError(f"Cloud Workspace was not found: {workspace_id}")
        name = str(selected.get("name") or "")
        self.repository.upsert_device_workspaces(
            session.user.id,
            device.id,
            [{"workspace_id": workspace_id, "local_path": str(target)}],
            session.access_token,
        )
        mapping = self.state.save_workspace_mapping(workspace_id, target, name)
        return mapping.to_dict()

    def list_workspaces(self) -> list[dict[str, Any]]:
        session = self.auth.restore_session()
        if session is None:
            raise RuntimeError("Sign in to Codex Sync before listing Workspaces")
        device = self.devices.current_device()
        cloud_workspaces = self.repository.list_workspaces(
            session.user.id,
            session.access_token,
        )
        cloud_mappings = {
            str(item.get("workspace_id") or ""): str(item.get("local_path") or "")
            for item in self.repository.list_device_workspaces(
                session.user.id,
                device.id,
                session.access_token,
            )
        }
        local_mappings = {
            item.workspace_id: item for item in self.state.list_workspace_mappings()
        }
        return [
            {
                "workspace_id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "local_path": (
                    local_mappings[str(item.get("id") or "")].local_path
                    if str(item.get("id") or "") in local_mappings
                    else None
                ),
                "cloud_device_path": cloud_mappings.get(str(item.get("id") or "")),
                "mapped": str(item.get("id") or "") in local_mappings,
                "updated_at": item.get("updated_at"),
            }
            for item in cloud_workspaces
        ]
