from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from codex_sync.cloud.native_manifest import NativeExportManifest
from codex_sync.cloud.native_state import (
    NativeCloudBundle,
    NativeCloudIntegrityError,
    NativeStateRepository,
    NativeStateRepositoryError,
)
from codex_sync.native.codec import NativeStateCodec
from codex_sync.native.export import export_native_state
from codex_sync.native.snapshot import NativeSnapshot, create_native_snapshot


@dataclass(frozen=True)
class NativeCloudBackupResult:
    export_id: str
    snapshot: NativeSnapshot
    manifest: NativeExportManifest
    export: object

    def to_dict(self) -> dict[str, object]:
        return {
            "export_id": self.export_id,
            "snapshot": self.snapshot.to_dict(),
            "manifest": self.manifest.to_dict(),
            "export": self.export.to_dict(),
        }


def link_snapshot_native_export(
    snapshot: Mapping[str, object],
    export_id: str,
) -> dict[str, object]:
    """Return a local snapshot payload linked to a Native cloud export."""

    linked = dict(snapshot)
    linked["native_export_id"] = export_id
    return linked


def _row_key(row: object) -> tuple[str, ...]:
    if hasattr(row, "source_project_id"):
        return ("project", str(row.source_project_id))
    if hasattr(row, "codex_thread_id"):
        return ("thread", str(row.codex_thread_id))
    if hasattr(row, "object_type") and hasattr(row, "object_key"):
        return ("related", str(row.object_type), str(row.object_key))
    if hasattr(row, "id"):
        return ("root", str(row.id))
    raise TypeError(f"Unsupported Native cloud row: {type(row).__name__}")


def _missing_rows(
    source: Sequence[object],
    existing: Sequence[object],
) -> list[object]:
    existing_by_key = {_row_key(row): row for row in existing}
    return [
        row
        for row in source
        if (
            _row_key(row) not in existing_by_key
            or existing_by_key[_row_key(row)].to_dict() != row.to_dict()
        )
    ]


class NativeCloudBackupService:
    """Snapshot, upload, verify, and complete Native cloud exports."""

    def __init__(
        self,
        repository: NativeStateRepository,
        *,
        user_id: str,
        source_database: Path,
        session_index_path: Path | None = None,
        batch_size: int = 100,
        source_device_id: str | None = None,
        source_codex_version: str | None = None,
        source_platform: str | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.repository = repository
        self.user_id = user_id
        self.source_database = Path(source_database)
        self.session_index_path = session_index_path
        self.batch_size = batch_size
        self.source_device_id = source_device_id
        self.source_codex_version = source_codex_version
        self.source_platform = source_platform

    def backup(
        self,
        *,
        resume_export_id: str | None = None,
        snapshot_destination: Path | None = None,
        label: str = "native",
    ) -> NativeCloudBackupResult:
        verify_schema = getattr(self.repository, "verify_schema", None)
        if callable(verify_schema):
            verify_schema()
        snapshot = create_native_snapshot(
            self.source_database,
            snapshot_destination,
            label=label,
        )
        local_export = export_native_state(
            snapshot.path,
            session_index_path=self.session_index_path,
        )
        bundle = NativeStateCodec.encode_export(
            local_export,
            user_id=self.user_id,
            export_id=resume_export_id,
            source_device_id=self.source_device_id,
            source_codex_version=self.source_codex_version,
            source_platform=self.source_platform,
        )
        manifest = NativeExportManifest.from_rows(
            bundle.export,
            bundle.projects,
            bundle.project_roots,
            bundle.threads,
            bundle.related_state,
        )
        metadata = {
            **dict(bundle.export.metadata or {}),
            "status": "created",
            "manifest": manifest.to_dict(),
        }
        bundle = replace(
            bundle,
            export=replace(bundle.export, metadata=metadata),
        )
        export_id = bundle.export.id
        created = False
        try:
            if resume_export_id:
                existing = self.repository.get_export(self.user_id, resume_export_id)
                if existing is None:
                    raise NativeStateRepositoryError(
                        f"Native export cannot be resumed: {resume_export_id}"
                    )
                if existing.is_complete:
                    raise NativeStateRepositoryError(
                        f"Native export is already complete: {resume_export_id}"
                    )
                existing_manifest = dict(existing.metadata or {}).get("manifest")
                if existing_manifest is not None:
                    NativeExportManifest.from_dict(existing_manifest).verify_export(
                        bundle.export
                    )
                bundle = replace(
                    bundle,
                    export=replace(
                        bundle.export,
                        created_at=existing.created_at,
                        metadata={
                            **dict(existing.metadata or {}),
                            "status": "uploading",
                            "manifest": manifest.to_dict(),
                        },
                    ),
                )
                self.repository.update_export_metadata(
                    self.user_id,
                    export_id,
                    dict(bundle.export.metadata or {}),
                )
            else:
                self.repository.create_export(bundle.export)
                created = True
                self.repository.update_export_metadata(
                    self.user_id,
                    export_id,
                    {
                        **metadata,
                        "status": "uploading",
                    },
                )

            self._upload_bundle(bundle)
            self.repository.update_export_metadata(
                self.user_id,
                export_id,
                {
                    **dict(bundle.export.metadata or {}),
                    "status": "verifying",
                },
            )
            actual = self._read_back(export_id)
            manifest.verify_rows(
                actual[0],
                actual[1],
                actual[2],
                actual[3],
                actual[4],
            )
            self.repository.update_export_metadata(
                self.user_id,
                export_id,
                {
                    **dict(bundle.export.metadata or {}),
                    "status": "complete",
                },
            )
            completed = self.repository.complete_export(self.user_id, export_id)
            return NativeCloudBackupResult(
                export_id=export_id,
                snapshot=snapshot,
                manifest=manifest,
                export=completed,
            )
        except Exception:
            if created or resume_export_id:
                try:
                    current = self.repository.get_export(self.user_id, export_id)
                    if current is not None and not current.is_complete:
                        self.repository.update_export_metadata(
                            self.user_id,
                            export_id,
                            {
                                **dict(current.metadata or {}),
                                "status": "failed",
                            },
                        )
                except Exception:
                    pass
            raise

    def _upload_bundle(self, bundle: NativeCloudBundle) -> None:
        export_id = bundle.export.id
        existing_projects = self.repository.list_projects(self.user_id, export_id)
        existing_roots = self.repository.list_project_roots(self.user_id, export_id)
        existing_threads = self.repository.list_threads(self.user_id, export_id)
        existing_related = self.repository.list_related_state(self.user_id, export_id)
        self.repository.upsert_projects(
            self.user_id,
            export_id,
            _missing_rows(bundle.projects, existing_projects),
            batch_size=self.batch_size,
        )
        self.repository.upsert_project_roots(
            self.user_id,
            export_id,
            _missing_rows(bundle.project_roots, existing_roots),
            batch_size=self.batch_size,
        )
        self.repository.upsert_threads(
            self.user_id,
            export_id,
            _missing_rows(bundle.threads, existing_threads),
            batch_size=self.batch_size,
        )
        self.repository.upsert_related_state(
            self.user_id,
            export_id,
            _missing_rows(bundle.related_state, existing_related),
            batch_size=self.batch_size,
        )

    def _read_back(self, export_id: str) -> tuple[object, list[object], list[object], list[object], list[object]]:
        export = self.repository.get_export(self.user_id, export_id)
        if export is None:
            raise NativeCloudIntegrityError(f"Native export disappeared: {export_id}")
        return (
            export,
            self.repository.list_projects(self.user_id, export_id),
            self.repository.list_project_roots(self.user_id, export_id),
            self.repository.list_threads(self.user_id, export_id),
            self.repository.list_related_state(self.user_id, export_id),
        )
