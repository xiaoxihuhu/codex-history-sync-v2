from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .schema import CodexSchemaInspector


def _session_meta(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        line = handle.readline()
    item = json.loads(line)
    payload = item.get("payload") if isinstance(item, dict) else None
    if not isinstance(payload, dict) or item.get("type") != "session_meta":
        raise RuntimeError(f"Session is missing session_meta: {path}")
    return payload


@dataclass(frozen=True)
class VisibilityReport:
    visible_ready_threads: tuple[str, ...]
    visibility_blocked_threads: dict[str, tuple[str, ...]]
    integrity_check: str

    def to_dict(self) -> dict[str, object]:
        return {
            "visible_ready_threads": list(self.visible_ready_threads),
            "visibility_blocked_threads": {
                key: list(value)
                for key, value in self.visibility_blocked_threads.items()
            },
            "integrity_check": self.integrity_check,
        }


class NativeVisibilityVerifier:
    """Validate Codex-native visibility prerequisites without writing."""

    def verify(
        self,
        database_path: Path,
        *,
        thread_ids: Iterable[str] | None = None,
        expected_project_roots: dict[str, str] | None = None,
    ) -> VisibilityReport:
        database = database_path.expanduser().resolve(strict=True)
        connection = sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        try:
            integrity = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            if integrity.lower() != "ok":
                return VisibilityReport((), {}, integrity)
            schema = CodexSchemaInspector.inspect(connection)
            if "threads" not in schema.tables:
                return VisibilityReport(
                    (),
                    {"<database>": ("threads_table_missing",)},
                    integrity,
                )
            columns = tuple(schema.tables["threads"].columns)
            selected = list(columns)
            rows = connection.execute(
                f'SELECT {", ".join(f"""\"{name}\"""" for name in selected)} '
                "FROM threads ORDER BY id"
            ).fetchall()
            requested = set(thread_ids or ())
            ready: list[str] = []
            blocked: dict[str, tuple[str, ...]] = {}
            for row in rows:
                thread_id = str(row["id"])
                if requested and thread_id not in requested:
                    continue
                reasons: list[str] = []
                if "archived" in row.keys() and bool(row["archived"]):
                    reasons.append("archived")
                if "preview" in row.keys() and not str(row["preview"] or "").strip():
                    reasons.append("preview_empty")
                elif "preview" not in row.keys():
                    reasons.append("preview_column_missing")

                project_id = row["project_id"] if "project_id" in row.keys() else None
                if project_id:
                    if "projects" not in schema.tables:
                        reasons.append("projects_table_missing")
                    else:
                        project = connection.execute(
                            "SELECT id FROM projects WHERE id = ?",
                            (project_id,),
                        ).fetchone()
                        if project is None:
                            reasons.append("project_missing")
                        elif "project_roots" not in schema.tables:
                            reasons.append("project_roots_table_missing")
                        else:
                            roots = connection.execute(
                                "SELECT path FROM project_roots WHERE project_id = ?",
                                (project_id,),
                            ).fetchall()
                            if not roots:
                                reasons.append("project_root_missing")
                            expected = (expected_project_roots or {}).get(thread_id)
                            if expected and not any(
                                str(root["path"]).casefold()
                                == str(expected).casefold()
                                for root in roots
                            ):
                                reasons.append("project_root_mismatch")

                rollout_path = str(row["rollout_path"] or "")
                session_path = Path(rollout_path)
                if not session_path.is_file():
                    reasons.append("rollout_missing")
                else:
                    try:
                        payload = _session_meta(session_path)
                    except (OSError, json.JSONDecodeError, RuntimeError):
                        reasons.append("session_meta_invalid")
                    else:
                        if str(payload.get("id") or "") != thread_id:
                            reasons.append("session_thread_id_mismatch")

                recency = None
                for name in ("recency_at_ms", "updated_at_ms", "updated_at"):
                    if name in row.keys() and row[name] not in (None, ""):
                        try:
                            recency = int(row[name])
                        except (TypeError, ValueError):
                            recency = 0
                        if recency:
                            break
                if not recency or recency <= 0:
                    reasons.append("recency_invalid")

                if "history_mode" in row.keys() and "name" in row.keys():
                    mode = str(row["history_mode"] or "legacy").casefold()
                    display_value = (
                        str(row["name"] or "").strip()
                        or str(row["title"] or "").strip()
                        or str(row["preview"] or "").strip()
                    )
                    if mode in {"legacy", "paginated"} and not display_value:
                        reasons.append("display_name_empty")

                if reasons:
                    blocked[thread_id] = tuple(dict.fromkeys(reasons))
                else:
                    ready.append(thread_id)
            return VisibilityReport(tuple(ready), blocked, integrity)
        finally:
            connection.close()
