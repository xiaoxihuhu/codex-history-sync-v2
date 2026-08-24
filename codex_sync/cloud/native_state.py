from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterable, Protocol

from codex_sync.native.codec import (
    NativeCloudBundle,
    NativeProjectRootRow,
    NativeProjectRow,
    NativeRelatedStateRow,
    NativeStateExportRow,
    NativeThreadRow,
    native_metadata_hash,
)

UTC = timezone.utc


class NativeStateRepository(Protocol):
    """Cloud persistence contract; this Phase intentionally has no HTTP writer."""

    def create_export(self, export: NativeStateExportRow) -> NativeStateExportRow: ...

    def upsert_projects(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeProjectRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeProjectRow]: ...

    def upsert_project_roots(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeProjectRootRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeProjectRootRow]: ...

    def upsert_threads(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeThreadRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeThreadRow]: ...

    def upsert_related_state(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeRelatedStateRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeRelatedStateRow]: ...

    def complete_export(self, user_id: str, export_id: str) -> NativeStateExportRow: ...

    def list_exports(self, user_id: str) -> list[NativeStateExportRow]: ...

    def get_latest_complete_export(
        self,
        user_id: str,
    ) -> NativeStateExportRow | None: ...

    def get_export(
        self,
        user_id: str,
        export_id: str,
    ) -> NativeStateExportRow | None: ...

    def list_projects(
        self,
        user_id: str,
        export_id: str,
    ) -> list[NativeProjectRow]: ...

    def list_project_roots(
        self,
        user_id: str,
        export_id: str,
    ) -> list[NativeProjectRootRow]: ...

    def list_threads(
        self,
        user_id: str,
        export_id: str,
    ) -> list[NativeThreadRow]: ...

    def list_related_state(
        self,
        user_id: str,
        export_id: str,
    ) -> list[NativeRelatedStateRow]: ...

    def delete_export(self, user_id: str, export_id: str) -> bool: ...


class EncryptedNativeSnapshotRepository(Protocol):
    """Future-only interface for encrypted Raw SQLite snapshots.

    Phase 10 never implements this method and never uploads SQLite bytes.
    """

    def upload_encrypted_snapshot(
        self,
        user_id: str,
        export_id: str,
        encrypted_payload: bytes,
    ) -> str: ...


class NativeStateRepositoryError(RuntimeError):
    pass


@dataclass
class _RepositoryState:
    exports: dict[tuple[str, str], NativeStateExportRow]
    projects: dict[tuple[str, str, str], NativeProjectRow]
    project_roots: dict[tuple[str, str, str], NativeProjectRootRow]
    threads: dict[tuple[str, str, str], NativeThreadRow]
    related_state: dict[tuple[str, str, str, str], NativeRelatedStateRow]


def _chunks(items: list[object], size: int) -> Iterable[list[object]]:
    if size <= 0:
        raise ValueError("batch_size must be positive")
    for start in range(0, len(items), size):
        yield items[start : start + size]


class InMemoryNativeStateRepository:
    """Deterministic local repository used by Phase 10 tests only."""

    def __init__(self) -> None:
        self._state = _RepositoryState({}, {}, {}, {}, {})
        self.batch_sizes: list[int] = []

    def create_export(self, export: NativeStateExportRow) -> NativeStateExportRow:
        key = (export.user_id, export.id)
        if key in self._state.exports:
            raise NativeStateRepositoryError(
                f"Native export already exists: {export.id}"
            )
        self._state.exports[key] = export
        return export

    def _export(self, user_id: str, export_id: str) -> NativeStateExportRow:
        export = self._state.exports.get((user_id, export_id))
        if export is None:
            raise NativeStateRepositoryError(
                f"Native export is not owned by user: {export_id}"
            )
        return export

    def _writable_export(
        self,
        user_id: str,
        export_id: str,
    ) -> NativeStateExportRow:
        export = self._export(user_id, export_id)
        if export.is_complete:
            raise NativeStateRepositoryError(
                f"Native export is immutable after completion: {export_id}"
            )
        return export

    def _record_batches(self, rows: list[object], batch_size: int) -> None:
        batches = list(_chunks(rows, batch_size))
        self.batch_sizes.extend(len(batch) for batch in batches)

    def upsert_projects(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeProjectRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeProjectRow]:
        self._writable_export(user_id, export_id)
        values = list(rows)
        self._record_batches(values, batch_size)
        for row in values:
            if row.user_id != user_id or row.export_id != export_id:
                raise NativeStateRepositoryError("Project ownership mismatch")
            self._state.projects[(user_id, export_id, row.source_project_id)] = row
        return values

    def upsert_project_roots(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeProjectRootRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeProjectRootRow]:
        self._writable_export(user_id, export_id)
        values = list(rows)
        self._record_batches(values, batch_size)
        project_ids = {
            row.id
            for row in self._state.projects.values()
            if row.user_id == user_id and row.export_id == export_id
        }
        for row in values:
            if row.user_id != user_id or row.export_id != export_id:
                raise NativeStateRepositoryError("Project root ownership mismatch")
            if row.native_project_id not in project_ids:
                raise NativeStateRepositoryError(
                    f"Project root references unknown project: {row.native_project_id}"
                )
            self._state.project_roots[(user_id, export_id, row.id)] = row
        return values

    def upsert_threads(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeThreadRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeThreadRow]:
        self._writable_export(user_id, export_id)
        values = list(rows)
        self._record_batches(values, batch_size)
        project_ids = {
            row.id
            for row in self._state.projects.values()
            if row.user_id == user_id and row.export_id == export_id
        }
        for row in values:
            if row.user_id != user_id or row.export_id != export_id:
                raise NativeStateRepositoryError("Thread ownership mismatch")
            if row.native_project_id is not None and row.native_project_id not in project_ids:
                raise NativeStateRepositoryError(
                    f"Thread references unknown project: {row.native_project_id}"
                )
            self._state.threads[(user_id, export_id, row.codex_thread_id)] = row
        return values

    def upsert_related_state(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeRelatedStateRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeRelatedStateRow]:
        self._writable_export(user_id, export_id)
        values = list(rows)
        self._record_batches(values, batch_size)
        for row in values:
            if row.user_id != user_id or row.export_id != export_id:
                raise NativeStateRepositoryError("Related state ownership mismatch")
            self._state.related_state[
                (user_id, export_id, row.object_type, row.object_key)
            ] = row
        return values

    def complete_export(self, user_id: str, export_id: str) -> NativeStateExportRow:
        export = self._writable_export(user_id, export_id)
        projects = self.list_projects(user_id, export_id)
        project_roots = self.list_project_roots(user_id, export_id)
        threads = self.list_threads(user_id, export_id)
        related_state = self.list_related_state(user_id, export_id)
        counts = {
            "projects": len(projects),
            "project_roots": len(project_roots),
            "threads": len(threads),
            "related_state": len(related_state),
        }
        expected = {
            "projects": export.project_count,
            "project_roots": export.project_root_count,
            "threads": export.thread_count,
            "related_state": export.related_state_count,
        }
        if counts != expected:
            raise NativeStateRepositoryError(
                f"Native export is incomplete: expected {expected}, found {counts}"
            )
        for row in threads:
            if native_metadata_hash(row.native_metadata) != row.metadata_hash:
                raise NativeStateRepositoryError(
                    f"Thread metadata hash mismatch: {row.codex_thread_id}"
                )
        for row in related_state:
            if native_metadata_hash(row.native_metadata) != row.metadata_hash:
                raise NativeStateRepositoryError(
                    f"Related metadata hash mismatch: {row.object_key}"
                )
        completed = replace(
            export,
            is_complete=True,
            completed_at=datetime.now(tz=UTC).isoformat(),
        )
        self._state.exports[(user_id, export_id)] = completed
        return completed

    def list_exports(self, user_id: str) -> list[NativeStateExportRow]:
        return sorted(
            (
                row
                for (owner, _), row in self._state.exports.items()
                if owner == user_id
            ),
            key=lambda row: (row.created_at, row.id),
            reverse=True,
        )

    def get_latest_complete_export(
        self,
        user_id: str,
    ) -> NativeStateExportRow | None:
        return next(
            (row for row in self.list_exports(user_id) if row.is_complete),
            None,
        )

    def get_export(
        self,
        user_id: str,
        export_id: str,
    ) -> NativeStateExportRow | None:
        return self._state.exports.get((user_id, export_id))

    def list_projects(self, user_id: str, export_id: str) -> list[NativeProjectRow]:
        if self.get_export(user_id, export_id) is None:
            return []
        return sorted(
            (
                row
                for (owner, current_export, _), row in self._state.projects.items()
                if owner == user_id and current_export == export_id
            ),
            key=lambda row: (row.position or 0, row.source_project_id),
        )

    def list_project_roots(
        self,
        user_id: str,
        export_id: str,
    ) -> list[NativeProjectRootRow]:
        if self.get_export(user_id, export_id) is None:
            return []
        return sorted(
            (
                row
                for (owner, current_export, _), row in self._state.project_roots.items()
                if owner == user_id and current_export == export_id
            ),
            key=lambda row: (row.position, row.source_path),
        )

    def list_threads(self, user_id: str, export_id: str) -> list[NativeThreadRow]:
        if self.get_export(user_id, export_id) is None:
            return []
        return sorted(
            (
                row
                for (owner, current_export, _), row in self._state.threads.items()
                if owner == user_id and current_export == export_id
            ),
            key=lambda row: row.codex_thread_id,
        )

    def list_related_state(
        self,
        user_id: str,
        export_id: str,
    ) -> list[NativeRelatedStateRow]:
        if self.get_export(user_id, export_id) is None:
            return []
        return sorted(
            (
                row
                for (owner, current_export, _, _), row in self._state.related_state.items()
                if owner == user_id and current_export == export_id
            ),
            key=lambda row: (row.object_type, row.object_key),
        )

    def delete_export(self, user_id: str, export_id: str) -> bool:
        key = (user_id, export_id)
        if key not in self._state.exports:
            return False
        del self._state.exports[key]
        for collection in (
            self._state.projects,
            self._state.project_roots,
            self._state.threads,
            self._state.related_state,
        ):
            for item_key in list(collection):
                if item_key[0] == user_id and item_key[1] == export_id:
                    del collection[item_key]
        return True

    def store_bundle(
        self,
        bundle: NativeCloudBundle,
        *,
        batch_size: int = 100,
    ) -> NativeStateExportRow:
        self.create_export(bundle.export)
        self.upsert_projects(
            bundle.export.user_id,
            bundle.export.id,
            bundle.projects,
            batch_size=batch_size,
        )
        self.upsert_project_roots(
            bundle.export.user_id,
            bundle.export.id,
            bundle.project_roots,
            batch_size=batch_size,
        )
        self.upsert_threads(
            bundle.export.user_id,
            bundle.export.id,
            bundle.threads,
            batch_size=batch_size,
        )
        self.upsert_related_state(
            bundle.export.user_id,
            bundle.export.id,
            bundle.related_state,
            batch_size=batch_size,
        )
        return self.complete_export(bundle.export.user_id, bundle.export.id)


class FakeSupabaseNativeStateRepository(InMemoryNativeStateRepository):
    """Named fake for contract tests; it performs no network requests."""
