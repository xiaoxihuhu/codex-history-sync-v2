from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from codex_sync.atomic_io import atomic_copy_file, atomic_write_bytes, atomic_write_stream
from codex_sync.hashing import sha256_bytes, sha256_file
from codex_sync.local.catalog import canonical_comparison_path
from codex_sync.local.repair_engine import (
    WRITE_LOCK_RETRY_DELAY_SECONDS,
    WRITE_LOCK_RETRY_LIMIT,
    WRITE_OPERATION_TIMEOUT_SECONDS,
    Paths,
    checkpoint,
    connect_db,
    ensure_environment,
    get_thread_columns,
    iso_utc_from_unix,
    is_locked_error,
    make_backup,
    parse_index_timestamp,
    parse_current_model,
    parse_current_provider,
    read_session_index,
    read_text,
    restore_database_with_retry,
    restore_metadata,
    split_first_line,
    sync_session_records,
    update_provider_assignments,
    write_session_index,
)

UTC = timezone.utc
ALLOWED_RESTORE_ROOTS = {"sessions", "archived_sessions"}
SAFE_SANDBOX_POLICY = json.dumps(
    {
        "type": "managed",
        "file_system": {
            "type": "restricted",
            "entries": [
                {
                    "path": {
                        "type": "special",
                        "value": {"kind": "root"},
                    },
                    "access": "read",
                }
            ],
        },
        "network": "restricted",
    },
    separators=(",", ":"),
)
SAFE_APPROVAL_MODE = "on-request"


@dataclass(frozen=True)
class PreparedTextRestore:
    cloud_thread_id: str
    codex_thread_id: str
    codex_session_id: str | None
    relative_path: str
    cloud_content_hash: str
    file_size: int
    content: bytes | None
    title: str
    source: str
    model_provider: str
    model: str | None
    archived: bool
    codex_created_at: str | None
    codex_updated_at: str | None
    target_cwd: Path
    index_thread_name: str | None = None
    index_updated_at: str | None = None
    content_path: Path | None = None
    replace_existing: bool = False


@dataclass(frozen=True)
class LocalRestoreSummary:
    selected_threads: int
    inserted_threads: int
    reconciled_threads: int
    created_sessions: int
    existing_sessions: int
    verified_threads: int
    verified_sessions: int
    rewritten_index_entries: int
    safety_backup: str | None
    session_meta_cwd_mismatches: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ThreadColumn:
    name: str
    not_null: bool
    default_value: object
    primary_key: bool


def safe_restore_path(codex_home: Path, relative_path: str) -> Path:
    if "\\" in relative_path:
        raise RuntimeError(f"Cloud Session path is not canonical: {relative_path}")
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or not pure.parts or pure.parts[0].lower() not in ALLOWED_RESTORE_ROOTS:
        raise RuntimeError(f"Cloud Session path is outside allowed rollout trees: {relative_path}")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise RuntimeError(f"Cloud Session path is unsafe: {relative_path}")

    home = codex_home.resolve(strict=True)
    target = codex_home.joinpath(*pure.parts)
    try:
        target.resolve(strict=False).relative_to(home)
    except ValueError as exc:
        raise RuntimeError(f"Cloud Session path escapes Codex home: {relative_path}") from exc
    return target


def parse_session_meta(content: bytes, expected_thread_id: str) -> tuple[dict[str, Any], str, str]:
    try:
        text = content.decode("utf-8")
        first_line, ending, remainder = split_first_line(text)
        item = json.loads(first_line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Session {expected_thread_id} is not valid UTF-8 JSONL") from exc
    if not isinstance(item, dict) or item.get("type") != "session_meta":
        raise RuntimeError(f"Session {expected_thread_id} is missing first-line session_meta")
    payload = item.get("payload")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Session {expected_thread_id} has invalid session_meta payload")
    actual_thread_id = str(payload.get("id") or "").strip()
    if actual_thread_id != expected_thread_id:
        raise RuntimeError(
            f"Session Thread ID mismatch: expected {expected_thread_id}, found {actual_thread_id}"
        )
    return payload, ending, remainder


def parse_session_meta_file(path: Path, expected_thread_id: str) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            first_line = handle.readline()
    except OSError as exc:
        raise RuntimeError(f"Session {expected_thread_id} could not be read: {path}") from exc
    payload, _, _ = parse_session_meta(first_line, expected_thread_id)
    return payload


def adapted_session_payload(
    payload: dict[str, Any],
    current_provider: str,
    current_model: str | None,
    target_cwd: Path,
) -> dict[str, Any]:
    adapted_payload = dict(payload)
    adapted_payload["model_provider"] = current_provider
    adapted_payload["cwd"] = str(target_cwd)
    if current_model and adapted_payload.get("model") is not None:
        adapted_payload["model"] = current_model
    return adapted_payload


def adapt_session_content(
    content: bytes,
    expected_thread_id: str,
    current_provider: str,
    current_model: str | None,
    target_cwd: Path,
) -> tuple[bytes, dict[str, Any]]:
    payload, ending, remainder = parse_session_meta(content, expected_thread_id)
    text = content.decode("utf-8")
    first_line, _, _ = split_first_line(text)
    item = json.loads(first_line)
    adapted_payload = adapted_session_payload(
        payload,
        current_provider,
        current_model,
        target_cwd,
    )
    item["payload"] = adapted_payload
    adapted_first_line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    if ending:
        adapted_text = adapted_first_line + ending + remainder
    else:
        adapted_text = adapted_first_line
    return adapted_text.encode("utf-8"), adapted_payload


def atomic_adapt_session_file(
    source: Path,
    target: Path,
    expected_thread_id: str,
    current_provider: str,
    current_model: str | None,
    target_cwd: Path,
) -> dict[str, Any]:
    payload = parse_session_meta_file(source, expected_thread_id)
    adapted_payload = adapted_session_payload(
        payload,
        current_provider,
        current_model,
        target_cwd,
    )

    def write(output: Any) -> None:
        with source.open("rb") as input_handle:
            first_line = input_handle.readline()
            if first_line.endswith(b"\r\n"):
                ending = b"\r\n"
            elif first_line.endswith(b"\n"):
                ending = b"\n"
            elif first_line.endswith(b"\r"):
                ending = b"\r"
            else:
                ending = b""
            try:
                item = json.loads(first_line[: len(first_line) - len(ending)].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Session {expected_thread_id} is not valid UTF-8 JSONL"
                ) from exc
            item["payload"] = adapted_payload
            output.write(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            output.write(ending)
            shutil.copyfileobj(input_handle, output, length=1024 * 1024)

    atomic_write_stream(target, write)
    return adapted_payload


def validate_cloud_session_content(entry: PreparedTextRestore) -> None:
    if entry.content_path is not None:
        actual_size = entry.content_path.stat().st_size
        if actual_size != entry.file_size:
            raise RuntimeError(
                f"Cloud Session size mismatch before restore: {entry.codex_thread_id}"
            )
        actual_hash = sha256_file(entry.content_path)
    else:
        content = entry.content
        if content is None:
            raise RuntimeError(f"Missing cloud content for Session {entry.codex_thread_id}")
        actual_size = len(content)
        if actual_size != entry.file_size:
            raise RuntimeError(
                f"Cloud Session size mismatch before restore: {entry.codex_thread_id}"
            )
        actual_hash = sha256_bytes(content)
    if actual_hash != entry.cloud_content_hash:
        raise RuntimeError(
            f"Cloud Session SHA256 mismatch before restore: {entry.codex_thread_id}"
        )


def canonical_session_cwd_matches(value: object, target_cwd: Path) -> bool:
    raw_value = str(value or "").strip()
    if not raw_value:
        return False
    try:
        return canonical_comparison_path(
            Path(raw_value),
            strict=False,
        ) == canonical_comparison_path(target_cwd, strict=False)
    except (OSError, ValueError):
        return raw_value.casefold() == str(target_cwd).casefold()


def adapt_selected_session_metadata(
    entries: list[PreparedTextRestore],
    target_paths: dict[str, Path],
    current_provider: str,
    current_model: str | None,
    *,
    reconcile_existing_thread_metadata: bool,
) -> int:
    adapted_files = 0
    selected_entries = (
        entries
        if reconcile_existing_thread_metadata
        else [entry for entry in entries if entry.replace_existing]
    )
    for entry in selected_entries:
        target = target_paths[entry.codex_thread_id]
        payload = parse_session_meta_file(target, entry.codex_thread_id)
        model_matches = (
            current_model is None
            or payload.get("model") is None
            or str(payload.get("model")) == current_model
        )
        if (
            canonical_session_cwd_matches(payload.get("cwd"), entry.target_cwd)
            and str(payload.get("model_provider") or "") == current_provider
            and model_matches
        ):
            continue
        atomic_adapt_session_file(
            target,
            target,
            entry.codex_thread_id,
            current_provider,
            current_model,
            entry.target_cwd,
        )
        adapted_files += 1
    return adapted_files


def parse_cloud_time(value: str | None) -> tuple[int, int]:
    if not value:
        now = datetime.now(tz=UTC)
    else:
        try:
            now = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError(f"Invalid cloud Thread timestamp: {value}") from exc
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
    return int(now.timestamp()), int(now.timestamp() * 1000)


def thread_columns(conn: sqlite3.Connection) -> dict[str, ThreadColumn]:
    return {
        str(row["name"]): ThreadColumn(
            name=str(row["name"]),
            not_null=bool(row["notnull"]),
            default_value=row["dflt_value"],
            primary_key=bool(row["pk"]),
        )
        for row in conn.execute("PRAGMA table_info(threads)")
    }


def compatibility_values(
    conn: sqlite3.Connection,
    columns: set[str],
) -> tuple[str, str]:
    selected = [name for name in ("sandbox_policy", "approval_mode") if name in columns]
    if not selected:
        return SAFE_SANDBOX_POLICY, SAFE_APPROVAL_MODE
    order = " ORDER BY updated_at DESC" if "updated_at" in columns else ""
    row = conn.execute(f"SELECT {', '.join(selected)} FROM threads{order} LIMIT 1").fetchone()
    sandbox = SAFE_SANDBOX_POLICY
    approval = SAFE_APPROVAL_MODE
    if row is not None:
        if "sandbox_policy" in selected and row["sandbox_policy"]:
            sandbox = str(row["sandbox_policy"])
        if "approval_mode" in selected and row["approval_mode"]:
            approval = str(row["approval_mode"])
    return sandbox, approval


def build_thread_values(
    entry: PreparedTextRestore,
    target_path: Path,
    target_cwd: Path,
    current_provider: str,
    current_model: str | None,
    session_meta: dict[str, Any],
    sandbox_policy: str,
    approval_mode: str,
) -> dict[str, object]:
    created_at, created_at_ms = parse_cloud_time(entry.codex_created_at)
    updated_at, updated_at_ms = parse_cloud_time(entry.codex_updated_at)
    return {
        "id": entry.codex_thread_id,
        "rollout_path": str(target_path),
        "created_at": created_at,
        "updated_at": updated_at,
        "source": entry.source or str(session_meta.get("source") or ""),
        "model_provider": current_provider,
        "cwd": str(target_cwd),
        "title": entry.title,
        "sandbox_policy": sandbox_policy,
        "approval_mode": approval_mode,
        "archived": int(entry.archived),
        "model": current_model or entry.model,
        "created_at_ms": created_at_ms,
        "updated_at_ms": updated_at_ms,
        "thread_source": str(session_meta.get("thread_source") or ""),
        "history_mode": str(session_meta.get("history_mode") or "legacy"),
    }


def build_reconcile_thread_values(
    entry: PreparedTextRestore,
    target_path: Path,
    target_cwd: Path,
    current_provider: str,
    current_model: str | None,
    session_meta: dict[str, Any],
) -> dict[str, object]:
    values: dict[str, object] = {
        "rollout_path": str(target_path),
        "cwd": str(target_cwd),
        "title": entry.title,
        "archived": int(entry.archived),
        "model_provider": current_provider,
    }
    source = entry.source or str(session_meta.get("source") or "")
    if source:
        values["source"] = source
    chosen_model = current_model or entry.model
    if chosen_model is not None:
        values["model"] = chosen_model
    thread_source = str(session_meta.get("thread_source") or "")
    if thread_source:
        values["thread_source"] = thread_source
    history_mode = str(session_meta.get("history_mode") or "")
    if history_mode:
        values["history_mode"] = history_mode
    if entry.codex_created_at:
        created_at, created_at_ms = parse_cloud_time(entry.codex_created_at)
        values["created_at"] = created_at
        values["created_at_ms"] = created_at_ms
    if entry.codex_updated_at:
        updated_at, updated_at_ms = parse_cloud_time(entry.codex_updated_at)
        values["updated_at"] = updated_at
        values["updated_at_ms"] = updated_at_ms
    return values


def validate_insert_values(
    columns: dict[str, ThreadColumn],
    values: dict[str, object],
) -> None:
    missing = [
        column.name
        for column in columns.values()
        if column.not_null
        and column.default_value is None
        and not column.primary_key
        and column.name not in values
    ]
    if missing:
        raise RuntimeError(
            "Unsupported Codex threads schema; required columns are unknown: "
            + ", ".join(sorted(missing))
        )


def apply_thread_changes_with_retry(
    paths: Paths,
    insert_rows: list[dict[str, object]],
    update_rows: list[tuple[str, dict[str, object]]],
    columns: dict[str, ThreadColumn],
) -> None:
    if not insert_rows and not update_rows:
        return
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(1, WRITE_LOCK_RETRY_LIMIT + 1):
        try:
            with connect_db(
                paths.db_path,
                readonly=False,
                timeout_seconds=WRITE_OPERATION_TIMEOUT_SECONDS,
            ) as conn:
                conn.execute("BEGIN IMMEDIATE")
                for values in insert_rows:
                    selected = [name for name in values if name in columns]
                    placeholders = ", ".join("?" for _ in selected)
                    names = ", ".join(f'"{name}"' for name in selected)
                    conn.execute(
                        f"INSERT INTO threads ({names}) VALUES ({placeholders})",
                        [values[name] for name in selected],
                    )
                for thread_id, values in update_rows:
                    selected = [name for name in values if name in columns]
                    if not selected:
                        continue
                    assignments = ", ".join(f'"{name}" = ?' for name in selected)
                    updated = conn.execute(
                        f"UPDATE threads SET {assignments} WHERE id = ?",
                        [*(values[name] for name in selected), thread_id],
                    ).rowcount
                    if updated != 1:
                        raise RuntimeError(
                            f"Existing Thread disappeared during cloud restore: {thread_id}"
                        )
                conn.commit()
                checkpoint(conn)
            return
        except sqlite3.OperationalError as exc:
            if not is_locked_error(exc):
                raise
            last_error = exc
            if attempt >= WRITE_LOCK_RETRY_LIMIT:
                raise RuntimeError("Codex database stayed busy during cloud restore") from exc
            time.sleep(WRITE_LOCK_RETRY_DELAY_SECONDS)
    raise RuntimeError("Database restore retry loop ended unexpectedly") from last_error


def _valid_existing_index_name(entry: dict[str, str] | None, thread_id: str) -> bool:
    if not entry:
        return False
    name = str(entry.get("thread_name") or "").strip()
    return bool(name and name != thread_id)


def rebuild_restored_session_index(
    paths: Paths,
    conn: sqlite3.Connection,
    entries: list[PreparedTextRestore],
) -> dict[str, int]:
    existing_entries = read_session_index(paths)
    selected_entries = {entry.codex_thread_id: entry for entry in entries}
    columns = get_thread_columns(conn)
    select_parts = ["id"]
    for name in ("title", "updated_at", "archived"):
        if name in columns:
            select_parts.append(name)
    rows = conn.execute(
        f"SELECT {', '.join(select_parts)} FROM threads ORDER BY id"
    ).fetchall()
    db_ids = {str(row["id"]) for row in rows}
    merged: list[dict[str, str]] = []

    for row in rows:
        thread_id = str(row["id"])
        selected = selected_entries.get(thread_id)
        existing_entry = existing_entries.get(thread_id)
        archived = bool(row["archived"]) if "archived" in row.keys() else False

        if selected is not None:
            if archived:
                continue
            if selected.index_thread_name:
                thread_name = selected.index_thread_name
            elif _valid_existing_index_name(existing_entry, thread_id):
                thread_name = str(existing_entry["thread_name"])
            elif "title" in row.keys() and row["title"]:
                thread_name = str(row["title"])
            else:
                thread_name = thread_id

            if selected.index_updated_at:
                updated_at = selected.index_updated_at
            elif selected.codex_updated_at:
                updated_at = selected.codex_updated_at
            else:
                db_updated_at = (
                    int(row["updated_at"])
                    if "updated_at" in row.keys() and row["updated_at"]
                    else 0
                )
                updated_at = iso_utc_from_unix(db_updated_at)
            merged.append(
                {
                    "id": thread_id,
                    "thread_name": thread_name,
                    "updated_at": updated_at,
                }
            )
            continue

        if existing_entry is not None:
            merged.append(existing_entry)
        elif not archived:
            title = (
                str(row["title"])
                if "title" in row.keys() and row["title"]
                else thread_id
            )
            updated_at = (
                int(row["updated_at"])
                if "updated_at" in row.keys() and row["updated_at"]
                else 0
            )
            merged.append(
                {
                    "id": thread_id,
                    "thread_name": title,
                    "updated_at": iso_utc_from_unix(updated_at),
                }
            )

    for thread_id, entry in existing_entries.items():
        if thread_id not in db_ids:
            merged.append(entry)

    merged.sort(key=lambda item: (parse_index_timestamp(item["updated_at"]), item["id"]))
    write_session_index(paths, merged)
    return {
        "rewritten_index_entries": len(merged),
        "missing_session_index_entries_before": len(db_ids - set(existing_entries)),
        "preserved_index_only_entries": len(set(existing_entries) - db_ids),
    }


def verify_restored_history(
    paths: Paths,
    entries: list[PreparedTextRestore],
    target_paths: dict[str, Path],
    *,
    cwd_thread_ids: set[str] | None = None,
) -> tuple[int, int, int]:
    with connect_db(paths.db_path, readonly=True) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")
        placeholders = ", ".join("?" for _ in entries)
        ids = [entry.codex_thread_id for entry in entries]
        columns = get_thread_columns(conn)
        selected_columns = ["id"]
        if "cwd" in columns:
            selected_columns.append("cwd")
        rows = conn.execute(
            f"SELECT {', '.join(selected_columns)} FROM threads WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        found_ids = {str(row["id"]) for row in rows}
        database_cwds = {
            str(row["id"]): row["cwd"]
            for row in rows
            if "cwd" in columns
        }
    missing_threads = set(ids) - found_ids
    if missing_threads:
        raise RuntimeError(f"Restore verification found {len(missing_threads)} missing Threads")

    verified_sessions = 0
    session_meta_cwd_mismatches = 0
    for entry in entries:
        target = target_paths[entry.codex_thread_id]
        if not target.is_file():
            raise RuntimeError(f"Restore verification found missing Session: {target}")
        payload = parse_session_meta_file(target, entry.codex_thread_id)
        if (
            entry.codex_thread_id in (cwd_thread_ids or set())
            and "cwd" in columns
            and not canonical_session_cwd_matches(
                database_cwds.get(entry.codex_thread_id),
                Path(str(payload.get("cwd") or "")),
            )
        ):
            session_meta_cwd_mismatches += 1
        verified_sessions += 1

    index = read_session_index(paths)
    missing_index = [
        entry.codex_thread_id
        for entry in entries
        if not entry.archived and entry.codex_thread_id not in index
    ]
    if missing_index:
        raise RuntimeError(f"Restore verification found {len(missing_index)} missing index entries")
    if session_meta_cwd_mismatches:
        raise RuntimeError(
            "Restore verification found "
            f"{session_meta_cwd_mismatches} session_meta cwd mismatches"
        )
    return len(found_ids), verified_sessions, session_meta_cwd_mismatches


def restore_text_history(
    paths: Paths,
    entries: list[PreparedTextRestore],
    *,
    reconcile_existing_thread_metadata: bool = False,
) -> LocalRestoreSummary:
    ensure_environment(paths)
    if not entries:
        return LocalRestoreSummary(0, 0, 0, 0, 0, 0, 0, 0, None)

    target_cwds = {
        entry.codex_thread_id: entry.target_cwd.expanduser().resolve(strict=False)
        for entry in entries
    }
    missing_targets = [
        target for target in target_cwds.values() if not target.is_dir()
    ]
    if missing_targets:
        raise RuntimeError(f"Target working directory does not exist: {missing_targets[0]}")
    config_text = read_text(paths.config_path)
    current_provider = parse_current_provider(config_text, paths)
    current_model = parse_current_model(config_text, paths)
    target_paths: dict[str, Path] = {}

    with connect_db(paths.db_path, readonly=True) as conn:
        if str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
            raise RuntimeError("Target Codex database failed integrity_check before restore")
        columns = thread_columns(conn)
        if not columns or "id" not in columns or "model_provider" not in columns:
            raise RuntimeError("Target Codex database has an unsupported threads table")
        column_names = get_thread_columns(conn)
        sandbox_policy, approval_mode = compatibility_values(conn, column_names)
        existing_select = [
            name
            for name in (
                "id",
                "rollout_path",
                "cwd",
                "title",
                "source",
                "archived",
                "created_at",
                "updated_at",
                "created_at_ms",
                "updated_at_ms",
                "model_provider",
                "model",
                "thread_source",
                "history_mode",
            )
            if name in column_names
        ]
        existing_rows = {
            str(row["id"]): row
            for row in conn.execute(f"SELECT {', '.join(existing_select)} FROM threads")
        }

    seen_paths: dict[Path, str] = {}
    insert_rows: list[dict[str, object]] = []
    update_rows: list[tuple[str, dict[str, object]]] = []
    files_to_create: list[tuple[Path, bytes | None, Path | None, PreparedTextRestore]] = []
    files_to_replace: list[tuple[Path, bytes | None, Path | None, PreparedTextRestore]] = []
    cwd_verification_ids: set[str] = set()
    existing_sessions = 0

    for entry in entries:
        target_cwd = target_cwds[entry.codex_thread_id]
        if entry.codex_thread_id in target_paths:
            raise RuntimeError(f"Cloud restore manifest duplicates Thread {entry.codex_thread_id}")
        target = safe_restore_path(paths.codex_home, entry.relative_path)
        previous_owner = seen_paths.get(target)
        if previous_owner and previous_owner != entry.codex_thread_id:
            raise RuntimeError(f"Cloud restore manifest reuses Session path {entry.relative_path}")
        seen_paths[target] = entry.codex_thread_id
        target_paths[entry.codex_thread_id] = target

        if target.exists():
            if entry.replace_existing:
                if entry.content_path is not None:
                    payload = parse_session_meta_file(entry.content_path, entry.codex_thread_id)
                else:
                    payload, _ending, _remainder = parse_session_meta(
                        entry.content or b"",
                        entry.codex_thread_id,
                    )
                files_to_replace.append(
                    (
                        target,
                        entry.content,
                        entry.content_path,
                        entry,
                    )
                )
                cwd_verification_ids.add(entry.codex_thread_id)
            else:
                payload = parse_session_meta_file(target, entry.codex_thread_id)
            existing_sessions += 1
        else:
            if entry.content is None and entry.content_path is None:
                raise RuntimeError(f"Missing downloaded content for Session {entry.codex_thread_id}")
            if entry.content_path is not None:
                payload = adapted_session_payload(
                    parse_session_meta_file(entry.content_path, entry.codex_thread_id),
                    current_provider,
                    current_model,
                    target_cwd,
                )
            else:
                adapted_content, payload = adapt_session_content(
                    entry.content or b"",
                    entry.codex_thread_id,
                    current_provider,
                    current_model,
                    target_cwd,
                )
                files_to_create.append((target, adapted_content, None, entry))
            if entry.content_path is not None:
                files_to_create.append((target, None, entry.content_path, entry))
            cwd_verification_ids.add(entry.codex_thread_id)

        existing = existing_rows.get(entry.codex_thread_id)
        if existing is not None:
            if (
                not reconcile_existing_thread_metadata
                and "rollout_path" in column_names
                and existing["rollout_path"]
            ):
                existing_path = Path(str(existing["rollout_path"]))
                try:
                    same_path = canonical_comparison_path(
                        existing_path,
                        strict=False,
                    ) == canonical_comparison_path(target, strict=False)
                except OSError:
                    same_path = False
                if not same_path:
                    raise RuntimeError(
                        f"Existing Thread {entry.codex_thread_id} points to a different Session"
                    )
            if reconcile_existing_thread_metadata:
                candidate_values = build_reconcile_thread_values(
                    entry,
                    target,
                    target_cwd,
                    current_provider,
                    current_model,
                    payload,
                )
                changed_values = {
                    name: value
                    for name, value in candidate_values.items()
                    if name in column_names and existing[name] != value
                }
                if changed_values:
                    update_rows.append((entry.codex_thread_id, changed_values))
            continue

        values = build_thread_values(
            entry,
            target,
            target_cwd,
            current_provider,
            current_model,
            payload,
            sandbox_policy,
            approval_mode,
        )
        validate_insert_values(columns, values)
        insert_rows.append(values)

    index_before = read_session_index(paths)
    needs_index_rebuild = reconcile_existing_thread_metadata or any(
        not entry.archived and entry.codex_thread_id not in index_before
        for entry in entries
    )
    has_mutations = bool(
        files_to_create
        or files_to_replace
        or insert_rows
        or update_rows
        or needs_index_rebuild
    )
    if not has_mutations:
        if reconcile_existing_thread_metadata:
            cwd_verification_ids = {entry.codex_thread_id for entry in entries}
        (
            verified_threads,
            verified_sessions,
            session_meta_cwd_mismatches,
        ) = verify_restored_history(
            paths,
            entries,
            target_paths,
            cwd_thread_ids=cwd_verification_ids,
        )
        return LocalRestoreSummary(
            selected_threads=len(entries),
            inserted_threads=0,
            reconciled_threads=0,
            created_sessions=0,
            existing_sessions=existing_sessions,
            verified_threads=verified_threads,
            verified_sessions=verified_sessions,
            rewritten_index_entries=len(index_before),
            safety_backup=None,
            session_meta_cwd_mismatches=session_meta_cwd_mismatches,
        )

    index_existed = paths.session_index_path.exists()
    safety_backup = make_backup(paths, "pre-cloud-restore")
    created_files: list[Path] = []
    replaced_backups: dict[Path, Path] = {}
    with tempfile.TemporaryDirectory(prefix="codex-sync-restore-rollback-") as rollback_dir:
        rollback_root = Path(rollback_dir)
        for index, (target, _content, _content_path, _entry) in enumerate(files_to_replace):
            backup_path = rollback_root / f"session-{index:06d}.jsonl"
            atomic_copy_file(target, backup_path)
            replaced_backups[target] = backup_path

        try:
            for target, content, content_path, entry in files_to_create:
                if target.exists():
                    raise RuntimeError(f"Session appeared during restore: {target}")
                if content_path is not None:
                    atomic_adapt_session_file(
                        content_path,
                        target,
                        entry.codex_thread_id,
                        current_provider,
                        current_model,
                        target_cwds[entry.codex_thread_id],
                    )
                else:
                    atomic_write_bytes(target, content or b"")
                created_files.append(target)

            for target, content, content_path, entry in files_to_replace:
                validate_cloud_session_content(entry)
                if content_path is not None:
                    atomic_adapt_session_file(
                        content_path,
                        target,
                        entry.codex_thread_id,
                        current_provider,
                        current_model,
                        target_cwds[entry.codex_thread_id],
                    )
                else:
                    if content is None:
                        raise RuntimeError(
                            f"Missing replacement content for Session {entry.codex_thread_id}"
                        )
                    adapted_content, _payload = adapt_session_content(
                        content,
                        entry.codex_thread_id,
                        current_provider,
                        current_model,
                        target_cwds[entry.codex_thread_id],
                    )
                    atomic_write_bytes(target, adapted_content)

            apply_thread_changes_with_retry(paths, insert_rows, update_rows, columns)
            update_provider_assignments(paths, current_provider, current_model)
            sync_session_records(paths, current_provider, current_model)
            adapt_selected_session_metadata(
                entries,
                target_paths,
                current_provider,
                current_model,
                reconcile_existing_thread_metadata=reconcile_existing_thread_metadata,
            )
            if reconcile_existing_thread_metadata:
                cwd_verification_ids = {entry.codex_thread_id for entry in entries}
            with connect_db(paths.db_path, readonly=True) as conn:
                index_summary = rebuild_restored_session_index(paths, conn, entries)
            (
                verified_threads,
                verified_sessions,
                session_meta_cwd_mismatches,
            ) = verify_restored_history(
                paths,
                entries,
                target_paths,
                cwd_thread_ids=cwd_verification_ids,
            )
        except Exception as exc:
            rollback_error: Exception | None = None
            try:
                restore_database_with_retry(paths, safety_backup)
                restore_metadata(paths, safety_backup)
                for path in created_files:
                    path.unlink(missing_ok=True)
                for path, backup_path in replaced_backups.items():
                    atomic_copy_file(backup_path, path)
                if not index_existed:
                    paths.session_index_path.unlink(missing_ok=True)
            except Exception as rollback_exc:
                rollback_error = rollback_exc
            if rollback_error is not None:
                raise RuntimeError(
                    f"Cloud restore failed and rollback also failed: {rollback_error}"
                ) from exc
            raise

    return LocalRestoreSummary(
        selected_threads=len(entries),
        inserted_threads=len(insert_rows),
        reconciled_threads=len(update_rows),
        created_sessions=len(created_files),
        existing_sessions=existing_sessions,
        verified_threads=verified_threads,
        verified_sessions=verified_sessions,
        rewritten_index_entries=index_summary["rewritten_index_entries"],
        safety_backup=str(safety_backup),
        session_meta_cwd_mismatches=session_meta_cwd_mismatches,
    )
