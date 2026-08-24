from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from codex_sync.cloud.native_state import (
    NativeCloudIntegrityError,
    NativeProjectRootRow,
    NativeProjectRow,
    NativeRelatedStateRow,
    NativeStateExportRow,
    NativeThreadRow,
    native_metadata_hash,
)


def _digest(items: Sequence[Mapping[str, object]]) -> str:
    canonical = json.dumps(
        sorted(
            (dict(item) for item in items),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NativeExportManifest:
    export_id: str
    format_version: int
    schema_fingerprint: str
    project_count: int
    project_root_count: int
    thread_count: int
    related_state_count: int
    threads_digest: str
    related_state_digest: str

    @classmethod
    def from_rows(
        cls,
        export: NativeStateExportRow,
        projects: Sequence[NativeProjectRow],
        project_roots: Sequence[NativeProjectRootRow],
        threads: Sequence[NativeThreadRow],
        related_state: Sequence[NativeRelatedStateRow],
    ) -> "NativeExportManifest":
        return cls(
            export_id=export.id,
            format_version=export.format_version,
            schema_fingerprint=export.codex_schema_fingerprint,
            project_count=len(projects),
            project_root_count=len(project_roots),
            thread_count=len(threads),
            related_state_count=len(related_state),
            threads_digest=_digest(
                [
                    {
                        "codex_thread_id": row.codex_thread_id,
                        "metadata_hash": row.metadata_hash,
                    }
                    for row in threads
                ]
            ),
            related_state_digest=_digest(
                [
                    {
                        "object_type": row.object_type,
                        "object_key": row.object_key,
                        "metadata_hash": row.metadata_hash,
                    }
                    for row in related_state
                ]
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "export_id": self.export_id,
            "format_version": self.format_version,
            "schema_fingerprint": self.schema_fingerprint,
            "counts": {
                "projects": self.project_count,
                "project_roots": self.project_root_count,
                "threads": self.thread_count,
                "related_state": self.related_state_count,
            },
            "threads_digest": self.threads_digest,
            "related_state_digest": self.related_state_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NativeExportManifest":
        counts = dict(payload.get("counts") or {})
        return cls(
            export_id=str(payload.get("export_id") or ""),
            format_version=int(payload.get("format_version") or 0),
            schema_fingerprint=str(payload.get("schema_fingerprint") or ""),
            project_count=int(counts.get("projects") or 0),
            project_root_count=int(counts.get("project_roots") or 0),
            thread_count=int(counts.get("threads") or 0),
            related_state_count=int(counts.get("related_state") or 0),
            threads_digest=str(payload.get("threads_digest") or ""),
            related_state_digest=str(payload.get("related_state_digest") or ""),
        )

    def verify_export(self, export: NativeStateExportRow) -> None:
        expected = {
            "export_id": export.id,
            "format_version": export.format_version,
            "schema_fingerprint": export.codex_schema_fingerprint,
            "project_count": export.project_count,
            "project_root_count": export.project_root_count,
            "thread_count": export.thread_count,
            "related_state_count": export.related_state_count,
        }
        actual = {
            "export_id": self.export_id,
            "format_version": self.format_version,
            "schema_fingerprint": self.schema_fingerprint,
            "project_count": self.project_count,
            "project_root_count": self.project_root_count,
            "thread_count": self.thread_count,
            "related_state_count": self.related_state_count,
        }
        if actual != expected:
            raise NativeCloudIntegrityError(
                f"Native export manifest mismatch: expected {expected}, found {actual}"
            )

    def verify_rows(
        self,
        export: NativeStateExportRow,
        projects: Sequence[NativeProjectRow],
        project_roots: Sequence[NativeProjectRootRow],
        threads: Sequence[NativeThreadRow],
        related_state: Sequence[NativeRelatedStateRow],
    ) -> None:
        self.verify_export(export)
        for row in threads:
            if native_metadata_hash(row.native_metadata) != row.metadata_hash:
                raise NativeCloudIntegrityError(
                    f"Native thread metadata hash mismatch: {row.codex_thread_id}"
                )
        for row in related_state:
            if native_metadata_hash(row.native_metadata) != row.metadata_hash:
                raise NativeCloudIntegrityError(
                    f"Native related-state metadata hash mismatch: {row.object_key}"
                )
        actual = NativeExportManifest.from_rows(
            export,
            projects,
            project_roots,
            threads,
            related_state,
        )
        if actual != self:
            raise NativeCloudIntegrityError(
                "Native cloud row count or digest verification failed"
            )


def manifest_from_export_metadata(export: NativeStateExportRow) -> NativeExportManifest:
    raw = dict(export.metadata or {}).get("manifest")
    if not isinstance(raw, Mapping):
        raise NativeCloudIntegrityError(
            f"Native export {export.id} has no integrity manifest"
        )
    manifest = NativeExportManifest.from_dict(raw)
    manifest.verify_export(export)
    return manifest
