from __future__ import annotations

import base64
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codex_sync.local.catalog import read_session_id

from .schema import CodexSchemaInspector, NativeSchemaReport

NATIVE_FORMAT_VERSION = 1
SECRET_COLUMN_PATTERN = re.compile(
    r"(?:token|secret|password|credential|cookie|api[_-]?key|service[_-]?role)",
    re.IGNORECASE,
)
SAFE_THREAD_FIELDS = {
    "id",
    "rollout_path",
    "created_at",
    "updated_at",
    "recency_at",
    "created_at_ms",
    "updated_at_ms",
    "recency_at_ms",
    "source",
    "history_mode",
    "thread_source",
    "agent_nickname",
    "agent_role",
    "agent_path",
    "model_provider",
    "model",
    "reasoning_effort",
    "cwd",
    "cli_version",
    "title",
    "name",
    "preview",
    "first_user_message",
    "sandbox_policy",
    "approval_mode",
    "tokens_used",
    "archived",
    "archived_at",
    "memory_mode",
    "project_id",
    "git_sha",
    "git_branch",
    "git_origin_url",
    "has_user_event",
    "is_pinned",
    "thread_section_id",
    "section_position",
    "section_entered_at_ms",
}


def _safe_column(name: str) -> bool:
    return not bool(SECRET_COLUMN_PATTERN.search(name))


def _json_value(value: object) -> object:
    if isinstance(value, bytes):
        return {
            "__codex_sync_type__": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    return value


def _restore_json_value(value: object) -> object:
    if (
        isinstance(value, dict)
        and value.get("__codex_sync_type__") == "bytes"
        and isinstance(value.get("base64"), str)
    ):
        return base64.b64decode(value["base64"])
    return value


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "message"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip())
                        break
        return "\n".join(parts).strip()
    return ""


def _first_user_message(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            next(handle, None)
            for line in handle:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = item.get("payload") if isinstance(item, dict) else None
                if not isinstance(payload, dict):
                    continue
                role = str(payload.get("role") or "").casefold()
                if role == "user":
                    message = _text_from_content(
                        payload.get("content")
                        or payload.get("message")
                        or payload.get("text")
                    )
                    if message:
                        return message
                event_type = str(payload.get("type") or "").casefold()
                if event_type in {"user_message", "user_input"}:
                    message = _text_from_content(
                        payload.get("message")
                        or payload.get("content")
                        or payload.get("text")
                    )
                    if message:
                        return message
    except (OSError, UnicodeDecodeError):
        return None
    return None


@dataclass(frozen=True)
class NativeThreadRecord:
    thread_id: str
    metadata: dict[str, object]

    @classmethod
    def from_row(
        cls,
        row: sqlite3.Row | dict[str, object],
        *,
        allowed_columns: set[str] | None = None,
    ) -> "NativeThreadRecord":
        values = dict(row)
        allowed = allowed_columns or set(values)
        metadata = {
            name: _json_value(value)
            for name, value in values.items()
            if name in allowed and _safe_column(name)
        }
        thread_id = str(metadata.get("id") or "").strip()
        if not thread_id:
            raise RuntimeError("Native Thread row is missing id")
        return cls(thread_id=thread_id, metadata=metadata)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.thread_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class NativeStateExport:
    format_version: int
    source: dict[str, object]
    projects: tuple[dict[str, object], ...]
    project_roots: tuple[dict[str, object], ...]
    threads: tuple[NativeThreadRecord, ...]
    related_state: dict[str, tuple[dict[str, object], ...]] = field(
        default_factory=dict
    )
    session_index: dict[str, dict[str, object]] = field(default_factory=dict)
    session_refs: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "source": self.source,
            "projects": [dict(item) for item in self.projects],
            "project_roots": [dict(item) for item in self.project_roots],
            "threads": [item.to_dict() for item in self.threads],
            "related_state": {
                name: [dict(row) for row in rows]
                for name, rows in self.related_state.items()
            },
            "session_index": self.session_index,
            "session_refs": [dict(item) for item in self.session_refs],
        }

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NativeStateExport":
        if int(payload.get("format_version") or 0) != NATIVE_FORMAT_VERSION:
            raise RuntimeError(
                f"Unsupported NativeStateExport format: {payload.get('format_version')}"
            )
        raw_threads = payload.get("threads")
        if not isinstance(raw_threads, list):
            raise RuntimeError("NativeStateExport is missing threads")
        threads = tuple(
            NativeThreadRecord(
                thread_id=str(item.get("id") or ""),
                metadata={
                    str(name): _restore_json_value(value)
                    for name, value in dict(item.get("metadata") or {}).items()
                },
            )
            for item in raw_threads
            if isinstance(item, dict)
        )
        related_raw = payload.get("related_state") or {}
        related = {
            str(name): tuple(
                {
                    str(key): _restore_json_value(value)
                    for key, value in dict(row).items()
                }
                for row in rows
                if isinstance(row, dict)
            )
            for name, rows in dict(related_raw).items()
            if isinstance(rows, list)
        }
        return cls(
            format_version=NATIVE_FORMAT_VERSION,
            source=dict(payload.get("source") or {}),
            projects=tuple(dict(item) for item in payload.get("projects") or []),
            project_roots=tuple(
                dict(item) for item in payload.get("project_roots") or []
            ),
            threads=threads,
            related_state=related,
            session_index=dict(payload.get("session_index") or {}),
            session_refs=tuple(
                dict(item) for item in payload.get("session_refs") or []
            ),
        )

    @classmethod
    def read_json(cls, path: Path) -> "NativeStateExport":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("NativeStateExport JSON must contain an object")
        return cls.from_dict(payload)


def _export_table_rows(
    connection: sqlite3.Connection,
    table: str,
    *,
    thread_ids: set[str] | None = None,
) -> tuple[dict[str, object], ...]:
    report = CodexSchemaInspector.inspect(connection)
    table_schema = report.tables.get(table)
    if table_schema is None:
        return ()
    columns = [
        name for name in table_schema.columns if _safe_column(name)
    ]
    if not columns:
        return ()
    quoted = table.replace('"', '""')
    rows = connection.execute(
        f'SELECT {", ".join(f"""\"{name.replace(chr(34), chr(34) * 2)}\"""" for name in columns)} '
        f'FROM "{quoted}"'
    ).fetchall()
    output = []
    for row in rows:
        item = {
            name: _json_value(row[name])
            for name in columns
        }
        if thread_ids is not None:
            candidate = str(
                item.get("thread_id")
                or item.get("parent_thread_id")
                or item.get("child_thread_id")
                or ""
            )
            if candidate and candidate not in thread_ids:
                continue
        output.append(item)
    return tuple(output)


def export_native_state(
    source_database: Path,
    *,
    session_index_path: Path | None = None,
) -> NativeStateExport:
    database = source_database.expanduser().resolve(strict=True)
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro",
        uri=True,
    )
    try:
        connection.row_factory = sqlite3.Row
        schema = CodexSchemaInspector.inspect(connection)
        if "threads" not in schema.tables:
            raise RuntimeError("Native database does not contain threads")

        thread_columns = {
            name
            for name in schema.tables["threads"].columns
            if name in SAFE_THREAD_FIELDS or _safe_column(name)
        }
        quoted_thread = [
            f'"{name.replace(chr(34), chr(34) * 2)}"'
            for name in schema.tables["threads"].columns
            if name in thread_columns and _safe_column(name)
        ]
        rows = connection.execute(
            f'SELECT {", ".join(quoted_thread)} FROM "threads" ORDER BY id'
        ).fetchall()
        threads = tuple(
            NativeThreadRecord.from_row(row, allowed_columns=thread_columns)
            for row in rows
        )
        thread_ids = {item.thread_id for item in threads}
        projects = _export_table_rows(connection, "projects")
        project_roots = _export_table_rows(connection, "project_roots")
        related: dict[str, tuple[dict[str, object], ...]] = {}
        for table in schema.related_tables:
            if table in {"threads", "projects", "project_roots"}:
                continue
            rows_for_table = _export_table_rows(
                connection,
                table,
                thread_ids=thread_ids,
            )
            if rows_for_table:
                related[table] = rows_for_table
    finally:
        connection.close()

    session_index: dict[str, dict[str, object]] = {}
    if session_index_path is not None and session_index_path.exists():
        for line in session_index_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict) and item.get("id"):
                session_index[str(item["id"])] = dict(item)

    session_refs: list[dict[str, object]] = []
    for thread in threads:
        rollout_path = thread.metadata.get("rollout_path")
        if not rollout_path:
            continue
        path = Path(str(rollout_path))
        if not path.is_file():
            continue
        session_refs.append(
            {
                "thread_id": thread.thread_id,
                "session_id": read_session_id(path, thread.thread_id),
                "rollout_path": str(path),
                "first_user_message": _first_user_message(path),
            }
        )

    return NativeStateExport(
        format_version=NATIVE_FORMAT_VERSION,
        source={
            "database": str(database),
            "schema": schema.to_dict(),
        },
        projects=projects,
        project_roots=project_roots,
        threads=threads,
        related_state=related,
        session_index=session_index,
        session_refs=tuple(session_refs),
    )
