from __future__ import annotations

import base64
import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import PurePath, PureWindowsPath
from typing import Any, Mapping

from .export import NATIVE_FORMAT_VERSION, NativeStateExport, NativeThreadRecord
from .policy import NativeCloudPolicy
from .schema import NativeSchemaFingerprint

UTC = timezone.utc
TRANSIENT_METADATA_KEYS = frozenset(
    {
        "backup_path",
        "database",
        "database_path",
        "hostname",
        "machine_name",
        "session_index_path",
        "temporary_path",
    }
)


class UnsupportedNativeStateFormatError(ValueError):
    """Raised when a cloud export uses a format this client cannot decode."""


def _canonical_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            if str(key) not in TRANSIENT_METADATA_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, bytes):
        return {
            "__codex_sync_type__": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, (PurePath, PureWindowsPath)):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Native metadata cannot contain non-finite floats")
    return value


def canonicalize_native_metadata(metadata: Mapping[str, object]) -> str:
    """Return deterministic UTF-8 JSON for safe Native metadata."""

    sanitized = NativeCloudPolicy.sanitize_row(metadata)
    return json.dumps(
        _canonical_value(sanitized),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def native_metadata_hash(metadata: Mapping[str, object]) -> str:
    return hashlib.sha256(
        canonicalize_native_metadata(metadata).encode("utf-8")
    ).hexdigest()


def _json_safe(value: object) -> object:
    if isinstance(value, bytes):
        return {
            "__codex_sync_type__": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _restore_json_safe(value: object) -> object:
    if (
        isinstance(value, dict)
        and value.get("__codex_sync_type__") == "bytes"
        and isinstance(value.get("base64"), str)
    ):
        return base64.b64decode(value["base64"])
    if isinstance(value, dict):
        return {str(key): _restore_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_json_safe(item) for item in value]
    return value


def _string(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _boolean(value: object) -> bool:
    return value not in (None, "", 0, False)


def _timestamp(value: object) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) > 10_000_000_000:
        return datetime.fromtimestamp(number / 1000, tz=UTC).isoformat()
    return datetime.fromtimestamp(number, tz=UTC).isoformat()


def _relative_rollout_path(value: object) -> str:
    text = str(value or "").replace("\\", "/").strip()
    marker = "/sessions/"
    if marker in text.casefold():
        index = text.casefold().rfind(marker)
        return text[index + 1 :].lstrip("/")
    if text.casefold().startswith("sessions/"):
        return text
    if not text:
        return "sessions/unknown.jsonl"
    return f"sessions/{PureWindowsPath(text).name or PurePath(text).name}"


def _schema_payload(source: Mapping[str, object]) -> dict[str, object]:
    raw = source.get("schema") or {}
    if not isinstance(raw, dict):
        raise ValueError("Native export source schema must be an object")
    payload = dict(raw)
    payload.pop("database", None)
    return payload


def _deterministic_uuid(export_id: str, kind: str, source_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"codex-history-sync:native:{export_id}:{kind}:{source_id}",
        )
    )


def _object_key(row: Mapping[str, object]) -> str:
    for key in ("id", "child_thread_id", "thread_id", "parent_thread_id"):
        if row.get(key) not in (None, ""):
            return str(row[key])
    return canonicalize_native_metadata(dict(row))


@dataclass(frozen=True)
class NativeStateExportRow:
    id: str
    user_id: str
    source_device_id: str | None
    format_version: int
    codex_schema_fingerprint: str
    codex_schema_json: dict[str, object]
    source_codex_version: str | None
    source_platform: str | None
    source_database_user_version: int | None
    source_database_application_id: int | None
    thread_count: int
    project_count: int
    project_root_count: int
    related_state_count: int
    created_at: str
    completed_at: str | None = None
    is_complete: bool = False
    metadata: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["metadata"] = dict(self.metadata or {})
        return payload


@dataclass(frozen=True)
class NativeProjectRow:
    id: str
    user_id: str
    export_id: str
    source_project_id: str
    name: str
    metadata: dict[str, object]
    position: int | None
    created_at_ms: int | None
    updated_at_ms: int | None
    native_metadata: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class NativeProjectRootRow:
    id: str
    user_id: str
    export_id: str
    native_project_id: str
    source_project_id: str
    position: int
    source_path: str
    normalized_source_path: str | None
    metadata: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class NativeThreadRow:
    id: str
    user_id: str
    export_id: str
    source_device_id: str | None
    codex_thread_id: str
    source_project_id: str | None
    native_project_id: str | None
    rollout_relative_path: str
    title: str | None
    name: str | None
    preview: str | None
    source: str | None
    history_mode: str | None
    model_provider: str | None
    model: str | None
    archived: bool
    codex_created_at: str | None
    codex_updated_at: str | None
    codex_recency_at: str | None
    native_metadata: dict[str, object]
    native_schema_columns: list[str]
    metadata_hash: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class NativeRelatedStateRow:
    id: str
    user_id: str
    export_id: str
    object_type: str
    source_table: str
    object_key: str
    native_metadata: dict[str, object]
    metadata_hash: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class NativeCloudBundle:
    export: NativeStateExportRow
    projects: tuple[NativeProjectRow, ...]
    project_roots: tuple[NativeProjectRootRow, ...]
    threads: tuple[NativeThreadRow, ...]
    related_state: tuple[NativeRelatedStateRow, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "export": self.export.to_dict(),
            "projects": [item.to_dict() for item in self.projects],
            "project_roots": [item.to_dict() for item in self.project_roots],
            "threads": [item.to_dict() for item in self.threads],
            "related_state": [item.to_dict() for item in self.related_state],
        }


class NativeStateCodec:
    """Translate local NativeStateExport values into cloud-safe rows."""

    @staticmethod
    def encode_project(
        project: Mapping[str, object],
        *,
        user_id: str,
        export_id: str,
        position: int,
    ) -> NativeProjectRow:
        safe = NativeCloudPolicy.sanitize_row(project)
        source_id = str(safe.get("id") or safe.get("source_project_id") or "").strip()
        if not source_id:
            raise ValueError("Native Project is missing source id")
        metadata = dict(safe)
        return NativeProjectRow(
            id=_deterministic_uuid(export_id, "project", source_id),
            user_id=user_id,
            export_id=export_id,
            source_project_id=source_id,
            name=str(safe.get("name") or source_id),
            metadata=metadata,
            position=int(safe["position"]) if safe.get("position") is not None else position,
            created_at_ms=(
                int(safe["created_at_ms"])
                if safe.get("created_at_ms") is not None
                else None
            ),
            updated_at_ms=(
                int(safe["updated_at_ms"])
                if safe.get("updated_at_ms") is not None
                else None
            ),
            native_metadata=metadata,
        )

    @staticmethod
    def encode_project_root(
        root: Mapping[str, object],
        *,
        user_id: str,
        export_id: str,
        project_id: str,
        source_project_id: str,
        position: int,
    ) -> NativeProjectRootRow:
        safe = NativeCloudPolicy.sanitize_row(root)
        source_path = str(safe.get("path") or safe.get("source_path") or "")
        if not source_path:
            raise ValueError("Native Project root is missing source path")
        return NativeProjectRootRow(
            id=_deterministic_uuid(
                export_id,
                "project-root",
                f"{source_project_id}:{position}:{source_path}",
            ),
            user_id=user_id,
            export_id=export_id,
            native_project_id=project_id,
            source_project_id=source_project_id,
            position=int(safe.get("position") or position),
            source_path=source_path,
            normalized_source_path=source_path.replace("\\", "/").casefold(),
            metadata=safe,
        )

    @staticmethod
    def encode_thread(
        thread: NativeThreadRecord,
        *,
        user_id: str,
        export_id: str,
        source_device_id: str | None,
        project_ids: Mapping[str, str],
    ) -> NativeThreadRow:
        safe = NativeCloudPolicy.sanitize_row(thread.metadata)
        source_project_id = _string(safe.get("project_id"))
        native_project_id = (
            project_ids.get(source_project_id)
            if source_project_id is not None
            else None
        )
        return NativeThreadRow(
            id=_deterministic_uuid(export_id, "thread", thread.thread_id),
            user_id=user_id,
            export_id=export_id,
            source_device_id=source_device_id,
            codex_thread_id=thread.thread_id,
            source_project_id=source_project_id,
            native_project_id=native_project_id,
            rollout_relative_path=_relative_rollout_path(
                safe.get("rollout_path")
            ),
            title=_string(safe.get("title")),
            name=_string(safe.get("name")),
            preview=_string(safe.get("preview")),
            source=_string(safe.get("source")),
            history_mode=_string(safe.get("history_mode")),
            model_provider=_string(safe.get("model_provider")),
            model=_string(safe.get("model")),
            archived=_boolean(safe.get("archived")),
            codex_created_at=_timestamp(
                safe.get("created_at") or safe.get("created_at_ms")
            ),
            codex_updated_at=_timestamp(
                safe.get("updated_at") or safe.get("updated_at_ms")
            ),
            codex_recency_at=_timestamp(
                safe.get("recency_at") or safe.get("recency_at_ms")
            ),
            native_metadata=safe,
            native_schema_columns=sorted(safe),
            metadata_hash=native_metadata_hash(safe),
        )

    @staticmethod
    def encode_related_state(
        source_table: str,
        row: Mapping[str, object],
        *,
        user_id: str,
        export_id: str,
    ) -> NativeRelatedStateRow:
        object_type = NativeCloudPolicy.object_type_for_table(source_table)
        safe = NativeCloudPolicy.sanitize_row(row)
        object_key = _object_key(safe)
        return NativeRelatedStateRow(
            id=_deterministic_uuid(
                export_id,
                object_type,
                object_key,
            ),
            user_id=user_id,
            export_id=export_id,
            object_type=object_type,
            source_table=source_table,
            object_key=object_key,
            native_metadata=safe,
            metadata_hash=native_metadata_hash(safe),
        )

    @classmethod
    def encode_export(
        cls,
        export: NativeStateExport,
        *,
        user_id: str,
        export_id: str | None = None,
        source_device_id: str | None = None,
        source_codex_version: str | None = None,
        source_platform: str | None = None,
    ) -> NativeCloudBundle:
        if export.format_version != NATIVE_FORMAT_VERSION:
            raise UnsupportedNativeStateFormatError(
                f"Unsupported NativeStateExport format: {export.format_version}"
            )
        export_id = export_id or str(uuid.uuid4())
        schema_json = _schema_payload(export.source)
        fingerprint = NativeSchemaFingerprint.from_payload(schema_json)
        projects = tuple(
            cls.encode_project(
                item,
                user_id=user_id,
                export_id=export_id,
                position=index,
            )
            for index, item in enumerate(export.projects)
        )
        project_ids = {
            item.source_project_id: item.id
            for item in projects
        }
        roots: list[NativeProjectRootRow] = []
        for index, item in enumerate(export.project_roots):
            safe = NativeCloudPolicy.sanitize_row(item)
            source_project_id = str(
                safe.get("project_id")
                or safe.get("source_project_id")
                or ""
            )
            project_id = project_ids.get(source_project_id)
            if project_id is None:
                raise ValueError(
                    f"Native Project root references unknown project: {source_project_id}"
                )
            roots.append(
                cls.encode_project_root(
                    safe,
                    user_id=user_id,
                    export_id=export_id,
                    project_id=project_id,
                    source_project_id=source_project_id,
                    position=index,
                )
            )
        threads = tuple(
            cls.encode_thread(
                item,
                user_id=user_id,
                export_id=export_id,
                source_device_id=source_device_id,
                project_ids=project_ids,
            )
            for item in export.threads
        )
        related: list[NativeRelatedStateRow] = []
        for source_table, rows in export.related_state.items():
            NativeCloudPolicy.assert_table_allowed(source_table)
            for row in rows:
                related.append(
                    cls.encode_related_state(
                        source_table,
                        row,
                        user_id=user_id,
                        export_id=export_id,
                    )
                )
        source = {
            key: value
            for key, value in export.source.items()
            if key not in TRANSIENT_METADATA_KEYS
        }
        source.pop("database", None)
        raw_schema = source.get("schema")
        if isinstance(raw_schema, dict):
            source["schema"] = _schema_payload(source)
        now = datetime.now(tz=UTC).isoformat()
        export_row = NativeStateExportRow(
            id=export_id,
            user_id=user_id,
            source_device_id=source_device_id,
            format_version=export.format_version,
            codex_schema_fingerprint=fingerprint.digest,
            codex_schema_json=schema_json,
            source_codex_version=source_codex_version,
            source_platform=source_platform,
            source_database_user_version=(
                int(schema_json.get("user_version"))
                if schema_json.get("user_version") is not None
                else None
            ),
            source_database_application_id=(
                int(schema_json.get("application_id"))
                if schema_json.get("application_id") is not None
                else None
            ),
            thread_count=len(threads),
            project_count=len(projects),
            project_root_count=len(roots),
            related_state_count=len(related),
            created_at=now,
            metadata={
                "source": _json_safe(source),
                "session_ref_count": len(export.session_refs),
            },
        )
        return NativeCloudBundle(
            export=export_row,
            projects=projects,
            project_roots=tuple(roots),
            threads=threads,
            related_state=tuple(related),
        )

    @staticmethod
    def decode_cloud_export(
        export: NativeStateExportRow,
        projects: list[NativeProjectRow] | tuple[NativeProjectRow, ...],
        project_roots: list[NativeProjectRootRow] | tuple[NativeProjectRootRow, ...],
        threads: list[NativeThreadRow] | tuple[NativeThreadRow, ...],
        related_state: list[NativeRelatedStateRow]
        | tuple[NativeRelatedStateRow, ...],
    ) -> NativeStateExport:
        if export.format_version != NATIVE_FORMAT_VERSION:
            raise UnsupportedNativeStateFormatError(
                f"Unsupported NativeStateExport format: {export.format_version}"
            )
        project_by_id = {item.id: item for item in projects}
        project_source_ids = {
            item.id: item.source_project_id for item in projects
        }
        decoded_projects = tuple(
            {
                **dict(item.native_metadata),
                "id": item.source_project_id,
                "name": item.name,
            }
            for item in projects
        )
        decoded_roots = tuple(
            {
                **dict(item.metadata),
                "project_id": item.source_project_id,
                "path": item.source_path,
                "position": item.position,
            }
            for item in project_roots
            if item.native_project_id in project_by_id
        )
        decoded_threads = tuple(
            NativeThreadRecord(
                thread_id=item.codex_thread_id,
                metadata={
                    **{
                        str(key): _restore_json_safe(value)
                        for key, value in item.native_metadata.items()
                    },
                    "id": item.codex_thread_id,
                    "project_id": (
                        project_source_ids.get(item.native_project_id)
                        if item.native_project_id
                        else item.source_project_id
                    ),
                },
            )
            for item in threads
        )
        related: dict[str, list[dict[str, object]]] = {}
        for item in related_state:
            related.setdefault(item.source_table, []).append(
                {
                    str(key): _restore_json_safe(value)
                    for key, value in item.native_metadata.items()
                }
            )
        raw_metadata = dict(export.metadata or {})
        source = dict(raw_metadata.get("source") or {})
        source["schema"] = dict(export.codex_schema_json)
        source.pop("database", None)
        return NativeStateExport(
            format_version=export.format_version,
            source=source,
            projects=decoded_projects,
            project_roots=decoded_roots,
            threads=decoded_threads,
            related_state={
                name: tuple(rows)
                for name, rows in related.items()
            },
        )
