from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import quote

from codex_sync.cloud.supabase_client import SupabaseClient, SupabaseError
from codex_sync.config import jwt_role
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
    """Cloud persistence contract for schema-aware Native row transport."""

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

    def update_export_metadata(
        self,
        user_id: str,
        export_id: str,
        metadata: Mapping[str, object],
    ) -> NativeStateExportRow: ...


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


class NativeAuthMismatchError(NativeStateRepositoryError):
    """The requested owner is not the current authenticated user."""


class NativeCloudIntegrityError(NativeStateRepositoryError):
    """Cloud counts, hashes, or manifests do not match."""


class NativeCloudSchemaNotInstalled(NativeStateRepositoryError):
    """Migration 010 is missing or does not expose required columns."""


class NativeCloudExportNotFound(NativeStateRepositoryError):
    pass


class NativeCloudExportIncomplete(NativeStateRepositoryError):
    pass


@dataclass(frozen=True)
class NativeCloudRetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 0.05
    max_delay_seconds: float = 2.0
    jitter: float = 0.0
    sleep: Callable[[float], None] = time.sleep

    def delay(self, attempt: int, error: SupabaseError) -> float:
        retry_after = next(
            (
                value
                for key, value in error.headers.items()
                if key.casefold() == "retry-after"
            ),
            None,
        )
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), self.max_delay_seconds))
            except ValueError:
                pass
        value = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** max(0, attempt - 1)),
        )
        if self.jitter:
            value += random.uniform(0.0, self.jitter)
        return value


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

    def update_export_metadata(
        self,
        user_id: str,
        export_id: str,
        metadata: Mapping[str, object],
    ) -> NativeStateExportRow:
        export = self._writable_export(user_id, export_id)
        updated = replace(export, metadata=dict(metadata))
        self._state.exports[(user_id, export_id)] = updated
        return updated

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


def _rows(payload: Any) -> list[dict[str, object]]:
    if payload in (None, {}):
        return []
    if isinstance(payload, dict):
        return [dict(payload)]
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    raise NativeStateRepositoryError("Supabase returned an invalid row payload")


def _decode_export(payload: Mapping[str, object]) -> NativeStateExportRow:
    return NativeStateExportRow(
        id=str(payload.get("id") or ""),
        user_id=str(payload.get("user_id") or ""),
        source_device_id=(
            str(payload["source_device_id"])
            if payload.get("source_device_id") is not None
            else None
        ),
        format_version=int(payload.get("format_version") or 0),
        codex_schema_fingerprint=str(payload.get("codex_schema_fingerprint") or ""),
        codex_schema_json=dict(payload.get("codex_schema_json") or {}),
        source_codex_version=(
            str(payload["source_codex_version"])
            if payload.get("source_codex_version") is not None
            else None
        ),
        source_platform=(
            str(payload["source_platform"])
            if payload.get("source_platform") is not None
            else None
        ),
        source_database_user_version=(
            int(payload["source_database_user_version"])
            if payload.get("source_database_user_version") is not None
            else None
        ),
        source_database_application_id=(
            int(payload["source_database_application_id"])
            if payload.get("source_database_application_id") is not None
            else None
        ),
        thread_count=int(payload.get("thread_count") or 0),
        project_count=int(payload.get("project_count") or 0),
        project_root_count=int(payload.get("project_root_count") or 0),
        related_state_count=int(payload.get("related_state_count") or 0),
        created_at=str(payload.get("created_at") or ""),
        completed_at=(
            str(payload["completed_at"])
            if payload.get("completed_at") is not None
            else None
        ),
        is_complete=bool(payload.get("is_complete")),
        metadata=dict(payload.get("metadata") or {}),
    )


def _decode_project(payload: Mapping[str, object]) -> NativeProjectRow:
    return NativeProjectRow(
        id=str(payload.get("id") or ""),
        user_id=str(payload.get("user_id") or ""),
        export_id=str(payload.get("export_id") or ""),
        source_project_id=str(payload.get("source_project_id") or ""),
        name=str(payload.get("name") or ""),
        metadata=dict(payload.get("metadata") or {}),
        position=int(payload["position"]) if payload.get("position") is not None else None,
        created_at_ms=(
            int(payload["created_at_ms"])
            if payload.get("created_at_ms") is not None
            else None
        ),
        updated_at_ms=(
            int(payload["updated_at_ms"])
            if payload.get("updated_at_ms") is not None
            else None
        ),
        native_metadata=dict(payload.get("native_metadata") or {}),
    )


def _decode_project_root(payload: Mapping[str, object]) -> NativeProjectRootRow:
    return NativeProjectRootRow(
        id=str(payload.get("id") or ""),
        user_id=str(payload.get("user_id") or ""),
        export_id=str(payload.get("export_id") or ""),
        native_project_id=str(payload.get("native_project_id") or ""),
        source_project_id=str(payload.get("source_project_id") or ""),
        position=int(payload.get("position") or 0),
        source_path=str(payload.get("source_path") or ""),
        normalized_source_path=(
            str(payload["normalized_source_path"])
            if payload.get("normalized_source_path") is not None
            else None
        ),
        metadata=dict(payload.get("metadata") or {}),
    )


def _decode_thread(payload: Mapping[str, object]) -> NativeThreadRow:
    return NativeThreadRow(
        id=str(payload.get("id") or ""),
        user_id=str(payload.get("user_id") or ""),
        export_id=str(payload.get("export_id") or ""),
        source_device_id=(
            str(payload["source_device_id"])
            if payload.get("source_device_id") is not None
            else None
        ),
        codex_thread_id=str(payload.get("codex_thread_id") or ""),
        source_project_id=(
            str(payload["source_project_id"])
            if payload.get("source_project_id") is not None
            else None
        ),
        native_project_id=(
            str(payload["native_project_id"])
            if payload.get("native_project_id") is not None
            else None
        ),
        rollout_relative_path=str(payload.get("rollout_relative_path") or ""),
        title=str(payload["title"]) if payload.get("title") is not None else None,
        name=str(payload["name"]) if payload.get("name") is not None else None,
        preview=str(payload["preview"]) if payload.get("preview") is not None else None,
        source=str(payload["source"]) if payload.get("source") is not None else None,
        history_mode=(
            str(payload["history_mode"])
            if payload.get("history_mode") is not None
            else None
        ),
        model_provider=(
            str(payload["model_provider"])
            if payload.get("model_provider") is not None
            else None
        ),
        model=str(payload["model"]) if payload.get("model") is not None else None,
        archived=bool(payload.get("archived")),
        codex_created_at=(
            str(payload["codex_created_at"])
            if payload.get("codex_created_at") is not None
            else None
        ),
        codex_updated_at=(
            str(payload["codex_updated_at"])
            if payload.get("codex_updated_at") is not None
            else None
        ),
        codex_recency_at=(
            str(payload["codex_recency_at"])
            if payload.get("codex_recency_at") is not None
            else None
        ),
        native_metadata=dict(payload.get("native_metadata") or {}),
        native_schema_columns=[
            str(item) for item in payload.get("native_schema_columns") or []
        ],
        metadata_hash=str(payload.get("metadata_hash") or ""),
    )


def _decode_related(payload: Mapping[str, object]) -> NativeRelatedStateRow:
    return NativeRelatedStateRow(
        id=str(payload.get("id") or ""),
        user_id=str(payload.get("user_id") or ""),
        export_id=str(payload.get("export_id") or ""),
        object_type=str(payload.get("object_type") or ""),
        source_table=str(payload.get("source_table") or ""),
        object_key=str(payload.get("object_key") or ""),
        native_metadata=dict(payload.get("native_metadata") or {}),
        metadata_hash=str(payload.get("metadata_hash") or ""),
    )


class SupabaseNativeStateRepository:
    """PostgREST repository bound to one authenticated user context."""

    _TABLES = {
        "exports": "native_state_exports",
        "projects": "native_projects",
        "roots": "native_project_roots",
        "threads": "native_threads",
        "related": "native_related_state",
    }

    def __init__(
        self,
        client: SupabaseClient,
        *,
        current_user_id: str | Callable[[], str],
        access_token: str | Callable[[], str],
        retry_policy: NativeCloudRetryPolicy | None = None,
    ) -> None:
        self.client = client
        self._current_user_id = current_user_id
        self._access_token = access_token
        self.retry_policy = retry_policy or NativeCloudRetryPolicy()

    def _user(self) -> str:
        value = (
            self._current_user_id()
            if callable(self._current_user_id)
            else self._current_user_id
        )
        if not str(value).strip():
            raise NativeAuthMismatchError("A signed-in Supabase user is required")
        return str(value)

    def _token(self) -> str:
        value = (
            self._access_token()
            if callable(self._access_token)
            else self._access_token
        )
        token = str(value).strip()
        if not token:
            raise NativeAuthMismatchError("A signed-in Supabase access token is required")
        if jwt_role(token) in {"service_role", "supabase_admin"}:
            raise NativeAuthMismatchError(
                "Native cloud transport requires a user access token, not service-role"
            )
        return token

    def _assert_user(self, user_id: str) -> None:
        if str(user_id) != self._user():
            raise NativeAuthMismatchError(
                "Requested Native rows do not belong to current user"
            )

    def verify_schema(self) -> dict[str, object]:
        return NativeCloudSchemaVerifier(
            self.client,
            access_token=self._token,
            retry_policy=self.retry_policy,
        ).verify()

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        expected_statuses: tuple[int, ...] = (200,),
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        last_error: SupabaseError | None = None
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                return self.client.request_json(
                    method,
                    path,
                    payload=payload,
                    access_token=self._token(),
                    extra_headers=extra_headers,
                    expected_statuses=expected_statuses,
                )
            except SupabaseError as exc:
                last_error = exc
                retryable = (
                    exc.status_code == 429
                    or exc.status_code is None
                    or exc.status_code >= 500
                )
                if not retryable or attempt >= self.retry_policy.max_attempts:
                    raise
                self.retry_policy.sleep(self.retry_policy.delay(attempt, exc))
            except (TimeoutError, ConnectionError, OSError) as exc:
                last_error = SupabaseError(
                    "Supabase network request failed",
                    cause=exc,
                )
                if attempt >= self.retry_policy.max_attempts:
                    raise last_error from exc
                self.retry_policy.sleep(
                    self.retry_policy.delay(attempt, last_error)
                )
        assert last_error is not None
        raise last_error

    @staticmethod
    def _query(path: str, **values: str) -> str:
        query = "&".join(
            f"{quote(key)}={quote(value, safe=',()')}"
            for key, value in values.items()
        )
        return f"{path}?{query}" if query else path

    def _owned(self, user_id: str, export_id: str) -> NativeStateExportRow:
        self._assert_user(user_id)
        export = self.get_export(user_id, export_id)
        if export is None:
            raise NativeStateRepositoryError(f"Native export not found: {export_id}")
        return export

    def create_export(self, export: NativeStateExportRow) -> NativeStateExportRow:
        self._assert_user(export.user_id)
        rows = _rows(
            self._request(
                "POST",
                f"/rest/v1/{self._TABLES['exports']}",
                payload=export.to_dict(),
                expected_statuses=(200, 201),
                extra_headers={"Prefer": "return=representation"},
            )
        )
        return _decode_export(rows[0] if rows else export.to_dict())

    def update_export_metadata(
        self,
        user_id: str,
        export_id: str,
        metadata: Mapping[str, object],
    ) -> NativeStateExportRow:
        current = self._owned(user_id, export_id)
        rows = _rows(
            self._request(
                "PATCH",
                self._query(
                    f"/rest/v1/{self._TABLES['exports']}",
                    user_id=f"eq.{user_id}",
                    id=f"eq.{export_id}",
                ),
                payload={"metadata": dict(metadata)},
                expected_statuses=(200, 204),
                extra_headers={"Prefer": "return=representation"},
            )
        )
        return _decode_export(
            rows[0]
            if rows
            else replace(current, metadata=dict(metadata)).to_dict()
        )

    def _upsert(
        self,
        table: str,
        user_id: str,
        export_id: str,
        rows: Iterable[object],
        *,
        batch_size: int,
        conflict: str,
    ) -> list[dict[str, object]]:
        self._owned(user_id, export_id)
        values = [row.to_dict() for row in rows]
        output: list[dict[str, object]] = []
        for batch in _chunks(values, batch_size):
            response = self._request(
                "POST",
                self._query(f"/rest/v1/{table}", on_conflict=conflict),
                payload=batch,
                expected_statuses=(200, 201),
                extra_headers={
                    "Prefer": "resolution=merge-duplicates,return=representation"
                },
            )
            output.extend(_rows(response) or batch)
        return output

    def upsert_projects(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeProjectRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeProjectRow]:
        return [
            _decode_project(item)
            for item in self._upsert(
                self._TABLES["projects"],
                user_id,
                export_id,
                rows,
                batch_size=batch_size,
                conflict="export_id,source_project_id",
            )
        ]

    def upsert_project_roots(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeProjectRootRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeProjectRootRow]:
        return [
            _decode_project_root(item)
            for item in self._upsert(
                self._TABLES["roots"],
                user_id,
                export_id,
                rows,
                batch_size=batch_size,
                conflict="native_project_id,position",
            )
        ]

    def upsert_threads(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeThreadRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeThreadRow]:
        return [
            _decode_thread(item)
            for item in self._upsert(
                self._TABLES["threads"],
                user_id,
                export_id,
                rows,
                batch_size=batch_size,
                conflict="export_id,codex_thread_id",
            )
        ]

    def upsert_related_state(
        self,
        user_id: str,
        export_id: str,
        rows: Iterable[NativeRelatedStateRow],
        *,
        batch_size: int = 100,
    ) -> list[NativeRelatedStateRow]:
        return [
            _decode_related(item)
            for item in self._upsert(
                self._TABLES["related"],
                user_id,
                export_id,
                rows,
                batch_size=batch_size,
                conflict="export_id,object_type,object_key",
            )
        ]

    def complete_export(self, user_id: str, export_id: str) -> NativeStateExportRow:
        current = self._owned(user_id, export_id)
        rows = _rows(
            self._request(
                "PATCH",
                self._query(
                    f"/rest/v1/{self._TABLES['exports']}",
                    user_id=f"eq.{user_id}",
                    id=f"eq.{export_id}",
                ),
                payload={
                    "is_complete": True,
                    "completed_at": datetime.now(tz=UTC).isoformat(),
                },
                expected_statuses=(200, 204),
                extra_headers={"Prefer": "return=representation"},
            )
        )
        return _decode_export(
            rows[0]
            if rows
            else replace(current, is_complete=True).to_dict()
        )

    def _list(
        self,
        table: str,
        user_id: str,
        export_id: str,
    ) -> list[dict[str, object]]:
        self._owned(user_id, export_id)
        return _rows(
            self._request(
                "GET",
                self._query(
                    f"/rest/v1/{table}",
                    select="*",
                    user_id=f"eq.{user_id}",
                    export_id=f"eq.{export_id}",
                    order="id.asc",
                ),
            )
        )

    def list_exports(self, user_id: str) -> list[NativeStateExportRow]:
        self._assert_user(user_id)
        return [
            _decode_export(item)
            for item in _rows(
                self._request(
                    "GET",
                    self._query(
                        f"/rest/v1/{self._TABLES['exports']}",
                        select="*",
                        user_id=f"eq.{user_id}",
                        order="created_at.desc,id.desc",
                    ),
                )
            )
        ]

    def get_latest_complete_export(
        self,
        user_id: str,
    ) -> NativeStateExportRow | None:
        return next(
            (item for item in self.list_exports(user_id) if item.is_complete),
            None,
        )

    def get_export(
        self,
        user_id: str,
        export_id: str,
    ) -> NativeStateExportRow | None:
        self._assert_user(user_id)
        rows = _rows(
            self._request(
                "GET",
                self._query(
                    f"/rest/v1/{self._TABLES['exports']}",
                    select="*",
                    user_id=f"eq.{user_id}",
                    id=f"eq.{export_id}",
                    limit="1",
                ),
            )
        )
        return _decode_export(rows[0]) if rows else None

    def list_projects(
        self,
        user_id: str,
        export_id: str,
    ) -> list[NativeProjectRow]:
        return [
            _decode_project(item)
            for item in self._list(self._TABLES["projects"], user_id, export_id)
        ]

    def list_project_roots(
        self,
        user_id: str,
        export_id: str,
    ) -> list[NativeProjectRootRow]:
        return [
            _decode_project_root(item)
            for item in self._list(self._TABLES["roots"], user_id, export_id)
        ]

    def list_threads(
        self,
        user_id: str,
        export_id: str,
    ) -> list[NativeThreadRow]:
        return [
            _decode_thread(item)
            for item in self._list(self._TABLES["threads"], user_id, export_id)
        ]

    def list_related_state(
        self,
        user_id: str,
        export_id: str,
    ) -> list[NativeRelatedStateRow]:
        return [
            _decode_related(item)
            for item in self._list(self._TABLES["related"], user_id, export_id)
        ]

    def delete_export(self, user_id: str, export_id: str) -> bool:
        self._owned(user_id, export_id)
        self._request(
            "DELETE",
            self._query(
                f"/rest/v1/{self._TABLES['exports']}",
                user_id=f"eq.{user_id}",
                id=f"eq.{export_id}",
            ),
            expected_statuses=(200, 204),
        )
        return True


class NativeCloudSchemaVerifier:
    """Fail-closed REST probe for migration 010."""

    _REQUIRED = {
        "native_state_exports": (
            "id,user_id,format_version,codex_schema_fingerprint,"
            "thread_count,project_count,project_root_count,related_state_count,"
            "is_complete,metadata"
        ),
        "native_projects": "id,user_id,export_id,source_project_id,native_metadata",
        "native_project_roots": (
            "id,user_id,export_id,native_project_id,source_project_id,source_path"
        ),
        "native_threads": (
            "id,user_id,export_id,codex_thread_id,native_metadata,metadata_hash"
        ),
        "native_related_state": (
            "id,user_id,export_id,object_type,object_key,native_metadata,metadata_hash"
        ),
    }

    def __init__(
        self,
        client: SupabaseClient,
        *,
        access_token: str | Callable[[], str],
        retry_policy: NativeCloudRetryPolicy | None = None,
    ) -> None:
        self.client = client
        self._access_token = access_token
        self.retry_policy = retry_policy or NativeCloudRetryPolicy()

    def verify(self) -> dict[str, object]:
        for table, columns in self._REQUIRED.items():
            try:
                self._request(
                    "GET",
                    f"/rest/v1/{table}?select={quote(columns, safe=',')}&limit=0",
                )
            except SupabaseError as exc:
                if exc.status_code in (400, 404):
                    raise NativeCloudSchemaNotInstalled(
                        f"Migration 010 is not installed or {table} is missing required columns"
                    ) from exc
                raise
        return {"installed": True, "tables": sorted(self._REQUIRED)}

    def _request(self, method: str, path: str) -> Any:
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                token = (
                    self._access_token()
                    if callable(self._access_token)
                    else self._access_token
                )
                token_text = str(token).strip()
                if not token_text:
                    raise NativeAuthMismatchError(
                        "A signed-in Supabase access token is required"
                    )
                if jwt_role(token_text) in {"service_role", "supabase_admin"}:
                    raise NativeAuthMismatchError(
                        "Native schema verification requires a user access token"
                    )
                return self.client.request_json(
                    method,
                    path,
                    access_token=token_text,
                    expected_statuses=(200,),
                )
            except SupabaseError as exc:
                retryable = (
                    exc.status_code == 429
                    or exc.status_code is None
                    or exc.status_code >= 500
                )
                if not retryable or attempt >= self.retry_policy.max_attempts:
                    raise
                self.retry_policy.sleep(self.retry_policy.delay(attempt, exc))
            except (TimeoutError, ConnectionError, OSError) as exc:
                error = SupabaseError(
                    "Supabase network request failed",
                    cause=exc,
                )
                if attempt >= self.retry_policy.max_attempts:
                    raise error from exc
                self.retry_policy.sleep(
                    self.retry_policy.delay(attempt, error)
                )
