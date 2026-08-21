"""Logical Workspace detection and per-device path mapping."""

from .manager import LogicalWorkspace, WorkspaceManager, workspace_id_for_path

__all__ = ["LogicalWorkspace", "WorkspaceManager", "workspace_id_for_path"]
