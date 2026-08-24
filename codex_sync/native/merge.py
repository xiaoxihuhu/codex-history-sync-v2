from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from typing import Any

from .export import NativeStateExport, NativeThreadRecord
from .projects import PathMappingSet, ProjectIdRemap, ProjectMergeEngine
from .schema import CodexSchemaInspector, NativeSchemaReport
from .snapshot import create_native_snapshot

UTC = timezone.utc
SAFE_SANDBOX_POLICY = json.dumps(
    {
        "type": "managed",
        "file_system": {"type": "restricted"},
        "network": "restricted",
    },
    separators=(",", ":"),
)


@dataclass(frozen=True)
class NativeMergeSummary:
    inserted_threads: int
    updated_threads: int
    preserved_threads: int
    created_projects: int
    reused_projects: int
    project_id_remap: ProjectIdRemap
    merged_related_rows: int
    safety_backup: str | None
    integrity_check: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["project_id_remap"] = self.project_id_remap.to_dict()
        return payload


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
        )
    }


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _truthy(value: object) -> bool:
    return value not in (None, "", 0, False)


class NativeMergeEngine:
    """Schema-aware, non-destructive merge of exported Codex native state."""

    def merge(
        self,
        source: NativeStateExport,
        target_database: Path,
        *,
        path_mapping: PathMappingSet = PathMappingSet(),
        safety_backup: Path | None = None,
    ) -> NativeMergeSummary:
        target = target_database.expanduser().resolve(strict=True)
        backup_path: Path | None = None
        if safety_backup is None:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            safety_backup = (
                target.parent
                / "history_sync_backups"
                / f"native-target-safety-{timestamp}.sqlite"
            )
        snapshot = create_native_snapshot(target, safety_backup, label="target-safety")
        backup_path = snapshot.path

        connection = sqlite3.connect(str(target))
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            target_schema = CodexSchemaInspector.inspect(connection)
            remap = ProjectMergeEngine().merge(
                connection,
                source.projects,
                source.project_roots,
                target_schema,
                path_mapping,
            )
            section_remap = self._merge_sections(
                connection,
                source.related_state.get("thread_sections", ()),
                target_schema,
            )
            inserted, updated, preserved = self._merge_threads(
                connection,
                source,
                target_schema,
                path_mapping,
                remap,
                section_remap,
            )
            related_rows = self._merge_related_state(
                connection,
                source,
                target_schema,
                remap,
                section_remap,
            )
            integrity = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            if integrity.lower() != "ok":
                connection.rollback()
                raise RuntimeError(
                    f"Native merge integrity check failed: {integrity}"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            connection.close()
            raise
        connection.close()

        with closing(
            sqlite3.connect(
                f"file:{target.as_posix()}?mode=ro",
                uri=True,
            )
        ) as verify:
            verify.row_factory = sqlite3.Row
            integrity = str(
                verify.execute("PRAGMA integrity_check").fetchone()[0]
            )
        if integrity.lower() != "ok":
            raise RuntimeError(f"Native merge readback failed: {integrity}")
        created_projects = sum(
            1
            for source_id, target_id in remap.source_to_target.items()
            if target_id and target_id != source_id
        )
        return NativeMergeSummary(
            inserted_threads=inserted,
            updated_threads=updated,
            preserved_threads=preserved,
            created_projects=created_projects,
            reused_projects=max(0, len(remap.source_to_target) - created_projects),
            project_id_remap=remap,
            merged_related_rows=related_rows,
            safety_backup=str(backup_path),
            integrity_check=integrity,
        )

    def _merge_sections(
        self,
        connection: sqlite3.Connection,
        source_rows: tuple[dict[str, object], ...] | list[dict[str, object]],
        target_schema: NativeSchemaReport,
    ) -> dict[str, str | None]:
        if not source_rows or "thread_sections" not in target_schema.tables:
            return {}
        columns = _columns(connection, "thread_sections")
        remap: dict[str, str | None] = {}
        for row in source_rows:
            source_id = str(row.get("id") or "")
            if not source_id:
                continue
            target = connection.execute(
                "SELECT id FROM thread_sections WHERE name = ? LIMIT 1",
                (row.get("name"),),
            ).fetchone()
            target_id = str(target[0]) if target else None
            if target_id is None:
                target_id = str(uuid.uuid4())
                values = {
                    key: value
                    for key, value in row.items()
                    if key in columns
                }
                values["id"] = target_id
                names = list(values)
                connection.execute(
                    f"INSERT INTO thread_sections ({', '.join(_quote(name) for name in names)}) "
                    f"VALUES ({', '.join('?' for _ in names)})",
                    [values[name] for name in names],
                )
            remap[source_id] = target_id
        return remap

    def _merge_threads(
        self,
        connection: sqlite3.Connection,
        source: NativeStateExport,
        target_schema: NativeSchemaReport,
        path_mapping: PathMappingSet,
        project_remap: ProjectIdRemap,
        section_remap: dict[str, str | None],
    ) -> tuple[int, int, int]:
        if "threads" not in target_schema.tables:
            raise RuntimeError("Target schema has no threads table")
        columns = _columns(connection, "threads")
        inserted = updated = preserved = 0
        for record in source.threads:
            source_values = dict(record.metadata)
            thread_id = record.thread_id
            source_values["id"] = thread_id
            for field in ("cwd", "rollout_path"):
                if field in source_values:
                    source_values[field] = path_mapping.map(source_values[field])
            if "project_id" in source_values:
                source_project = source_values.get("project_id")
                if source_project:
                    source_values["project_id"] = project_remap.target_id(
                        source_project
                    )
            if "thread_section_id" in source_values:
                source_values["thread_section_id"] = section_remap.get(
                    str(source_values["thread_section_id"])
                )
            existing = connection.execute(
                f'SELECT {", ".join(_quote(name) for name in columns)} '
                "FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
            if existing is None:
                self._fill_thread_fallbacks(source_values, source, thread_id)
                values = {
                    name: source_values[name]
                    for name in columns
                    if name in source_values
                }
                required = target_schema.tables["threads"].required_without_default
                missing = [name for name in required if name not in values]
                if missing:
                    raise RuntimeError(
                        "Cannot insert Thread "
                        f"{thread_id}; required target fields missing: "
                        + ", ".join(sorted(missing))
                    )
                names = list(values)
                connection.execute(
                    f"INSERT INTO threads ({', '.join(_quote(name) for name in names)}) "
                    f"VALUES ({', '.join('?' for _ in names)})",
                    [values[name] for name in names],
                )
                inserted += 1
                continue

            changed: dict[str, object] = {}
            for name, value in source_values.items():
                if name not in columns or name == "id":
                    continue
                if value is None and existing[name] is not None:
                    continue
                if name in {"preview", "name", "title", "first_user_message"}:
                    if not str(value or "").strip() and str(existing[name] or "").strip():
                        continue
                if existing[name] != value:
                    changed[name] = value
            if not changed:
                preserved += 1
                continue
            assignments = ", ".join(
                f"{_quote(name)} = ?" for name in changed
            )
            connection.execute(
                f"UPDATE threads SET {assignments} WHERE id = ?",
                [*changed.values(), thread_id],
            )
            updated += 1
        return inserted, updated, preserved

    def _fill_thread_fallbacks(
        self,
        values: dict[str, object],
        source: NativeStateExport,
        thread_id: str,
    ) -> None:
        if not str(values.get("preview") or "").strip():
            for candidate in (
                values.get("first_user_message"),
                values.get("title"),
                source.session_index.get(thread_id, {}).get("thread_name"),
                next(
                    (
                        item.get("first_user_message")
                        for item in source.session_refs
                        if str(item.get("thread_id") or "") == thread_id
                    ),
                    None,
                ),
                values.get("name"),
                thread_id,
            ):
                if str(candidate or "").strip():
                    values["preview"] = str(candidate).strip()
                    break
        if "name" in values and not str(values.get("name") or "").strip():
            values["name"] = values.get("title") or values.get("preview") or thread_id
        if "created_at_ms" not in values and values.get("created_at") is not None:
            values["created_at_ms"] = int(values["created_at"]) * 1000
        if "updated_at_ms" not in values and values.get("updated_at") is not None:
            values["updated_at_ms"] = int(values["updated_at"]) * 1000
        if "recency_at" not in values:
            values["recency_at"] = values.get("updated_at") or 0
        if "recency_at_ms" not in values:
            values["recency_at_ms"] = (
                values.get("updated_at_ms")
                or int(values.get("recency_at") or 0) * 1000
            )
        values.setdefault("source", "history-sync")
        values.setdefault("model_provider", "openai")
        values.setdefault("title", values.get("preview") or thread_id)
        values.setdefault("created_at", 0)
        values.setdefault("updated_at", values.get("created_at") or 0)
        values.setdefault("sandbox_policy", SAFE_SANDBOX_POLICY)
        values.setdefault("approval_mode", "on-request")
        values.setdefault("archived", 0)

    def _merge_related_state(
        self,
        connection: sqlite3.Connection,
        source: NativeStateExport,
        target_schema: NativeSchemaReport,
        project_remap: ProjectIdRemap,
        section_remap: dict[str, str | None],
    ) -> int:
        merged = 0
        for table, rows in source.related_state.items():
            if table in {"thread_sections", "projects", "project_roots"}:
                continue
            if not _table_exists(connection, table):
                continue
            columns = _columns(connection, table)
            if not rows:
                continue
            primary_keys = [
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
                )
                if int(row[5])
            ]
            if not primary_keys:
                continue
            for raw in rows:
                row = dict(raw)
                if "thread_id" in row:
                    row["thread_id"] = str(row["thread_id"])
                if "parent_thread_id" in row:
                    row["parent_thread_id"] = str(row["parent_thread_id"])
                if "child_thread_id" in row:
                    row["child_thread_id"] = str(row["child_thread_id"])
                if "project_id" in row and row["project_id"]:
                    row["project_id"] = project_remap.target_id(row["project_id"])
                if "thread_section_id" in row and row["thread_section_id"]:
                    row["thread_section_id"] = section_remap.get(
                        str(row["thread_section_id"])
                    )
                values = {
                    name: row[name] for name in row if name in columns
                }
                key_values = [values.get(name) for name in primary_keys]
                existing = connection.execute(
                    f'SELECT 1 FROM "{table}" WHERE '
                    + " AND ".join(f"{_quote(name)} = ?" for name in primary_keys)
                    + " LIMIT 1",
                    key_values,
                ).fetchone()
                if existing:
                    continue
                names = list(values)
                connection.execute(
                    f'INSERT INTO "{table}" ({", ".join(_quote(name) for name in names)}) '
                    f'VALUES ({", ".join("?" for _ in names)})',
                    [values[name] for name in names],
                )
                merged += 1
        return merged
