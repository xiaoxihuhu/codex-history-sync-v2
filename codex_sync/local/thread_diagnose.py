from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codex_sync.local.repair_engine import (
    Paths,
    connect_db,
    ensure_environment,
    get_thread_columns,
    read_session_index,
)

THREAD_FIELDS = (
    "title",
    "source",
    "archived",
    "cwd",
    "rollout_path",
    "model_provider",
    "model",
    "created_at",
    "updated_at",
    "created_at_ms",
    "updated_at_ms",
    "thread_source",
    "history_mode",
    "sandbox_policy",
    "approval_mode",
)

SESSION_META_FIELDS = (
    "id",
    "source",
    "thread_source",
    "history_mode",
    "cwd",
    "model_provider",
    "model",
)


def _read_first_session_meta(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        first_line = handle.readline().rstrip("\r\n")
    if not first_line:
        raise RuntimeError(f"Session is empty: {path}")
    item = json.loads(first_line)
    payload = item.get("payload") if isinstance(item, dict) else None
    if not isinstance(item, dict) or item.get("type") != "session_meta" or not isinstance(payload, dict):
        raise RuntimeError(f"Session is missing first-line session_meta: {path}")
    return payload


def diagnose_thread(paths: Paths, thread_id: str) -> dict[str, object]:
    ensure_environment(paths)
    database: dict[str, object] = {"exists": False}
    database.update({field: None for field in THREAD_FIELDS})

    with connect_db(paths.db_path, readonly=True) as conn:
        columns = get_thread_columns(conn)
        selected = ["id", *(field for field in THREAD_FIELDS if field in columns)]
        row = conn.execute(
            f"SELECT {', '.join(selected)} FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()

    if row is not None:
        database["exists"] = True
        for field in THREAD_FIELDS:
            if field in row.keys():
                database[field] = bool(row[field]) if field == "archived" else row[field]

    index_entry = read_session_index(paths).get(thread_id)
    session_index = {
        "exists": index_entry is not None,
        "thread_name": index_entry.get("thread_name") if index_entry else None,
        "updated_at": index_entry.get("updated_at") if index_entry else None,
    }

    session_meta: dict[str, object] = {"exists": False}
    session_meta.update({field: None for field in SESSION_META_FIELDS})
    rollout_path = database.get("rollout_path")
    if rollout_path:
        path = Path(str(rollout_path))
        if path.is_file():
            payload = _read_first_session_meta(path)
            session_meta["exists"] = True
            for field in SESSION_META_FIELDS:
                session_meta[field] = payload.get(field)

    return {
        "action": "thread-diagnose",
        "codex_home": str(paths.codex_home),
        "db_path": str(paths.db_path),
        "thread_id": thread_id,
        "database": database,
        "session_index": session_index,
        "session_meta": session_meta,
    }
