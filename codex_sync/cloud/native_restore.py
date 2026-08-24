from __future__ import annotations

from dataclasses import dataclass

from codex_sync.cloud.native_manifest import (
    NativeExportManifest,
    manifest_from_export_metadata,
)
from codex_sync.cloud.native_state import (
    NativeCloudExportIncomplete,
    NativeCloudExportNotFound,
    NativeStateRepository,
)
from codex_sync.native.codec import NativeStateCodec
from codex_sync.native.export import NativeStateExport


@dataclass(frozen=True)
class NativeCloudRestoreResult:
    export: object
    manifest: NativeExportManifest
    native_export: NativeStateExport

    def to_dict(self) -> dict[str, object]:
        return {
            "export": self.export.to_dict(),
            "manifest": self.manifest.to_dict(),
            "native_export": self.native_export.to_dict(),
        }


class NativeCloudRestoreService:
    """Download and decode a complete export without writing a target database."""

    def __init__(
        self,
        repository: NativeStateRepository,
        *,
        user_id: str,
    ) -> None:
        self.repository = repository
        self.user_id = user_id

    def restore(
        self,
        *,
        export_id: str | None = None,
    ) -> NativeCloudRestoreResult:
        verify_schema = getattr(self.repository, "verify_schema", None)
        if callable(verify_schema):
            verify_schema()
        export = (
            self.repository.get_latest_complete_export(self.user_id)
            if export_id is None
            else self.repository.get_export(self.user_id, export_id)
        )
        if export is None:
            raise NativeCloudExportNotFound(
                export_id or "No complete Native export exists"
            )
        if not export.is_complete:
            raise NativeCloudExportIncomplete(export.id)
        projects = self.repository.list_projects(self.user_id, export.id)
        project_roots = self.repository.list_project_roots(self.user_id, export.id)
        threads = self.repository.list_threads(self.user_id, export.id)
        related_state = self.repository.list_related_state(self.user_id, export.id)
        manifest = manifest_from_export_metadata(export)
        manifest.verify_rows(
            export,
            projects,
            project_roots,
            threads,
            related_state,
        )
        decoded = NativeStateCodec.decode_cloud_export(
            export,
            projects,
            project_roots,
            threads,
            related_state,
        )
        return NativeCloudRestoreResult(
            export=export,
            manifest=manifest,
            native_export=decoded,
        )
