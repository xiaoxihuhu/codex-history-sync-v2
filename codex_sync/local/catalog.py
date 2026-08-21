from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from codex_sync.hashing import sha256_bytes
from codex_sync.local.repair_engine import Paths, connect_db, get_thread_columns

UTC = timezone.utc
ALLOWED_SESSION_ROOTS = {"sessions", "archived_sessions"}


@dataclass(frozen=True)
class LocalSessionAsset:
    codex_thread_id: str
    codex_session_id: str
    relative_path: str
    path: Path
    file_size: int
    mtime_ns: int


@dataclass(frozen=True)
class LocalThreadRecord:
    codex_thread_id: str
    title: str
    source: str
    model_provider: str
    model: str | None
    original_cwd: str | None
    archived: bool
    codex_created_at: str | None
    codex_updated_at: str | None
    rollout_relative_path: str
    session: LocalSessionAsset


@dataclass(frozen=True)
class StableFile:
    content: bytes
    sha256: str
    file_size: int
    mtime_ns: int


def unix_timestamp_to_iso(value: object, *, milliseconds: bool = False) -> str | None:
    if value is None:
        return None
    number = float(value)
    if number <= 0:
        return None
    if milliseconds or number > 100_000_000_000:
        number /= 1000
    return datetime.fromtimestamp(number, tz=UTC).isoformat()


def canonical_comparison_path(path: Path, *, strict: bool = True) -> Path:
    raw_path = str(path)
    if os.name == "nt":
        if raw_path.startswith("\\\\?\\UNC\\"):
            raw_path = "\\\\" + raw_path[8:]
        elif raw_path.startswith("\\\\?\\"):
            raw_path = raw_path[4:]
    return Path(raw_path).resolve(strict=strict)


def relative_session_path(codex_home: Path, rollout_path: Path) -> str:
    canonical_home = canonical_comparison_path(codex_home)
    canonical_rollout = canonical_comparison_path(rollout_path)
    try:
        relative = canonical_rollout.relative_to(canonical_home)
    except ValueError as exc:
        raise RuntimeError(f"Session path is outside Codex home: {rollout_path}") from exc
    if not relative.parts or relative.parts[0].lower() not in ALLOWED_SESSION_ROOTS:
        raise RuntimeError(f"Session path is outside the allowed rollout trees: {rollout_path}")
    return relative.as_posix()


def session_id_from_payload(payload: dict[str, object], fallback_thread_id: str) -> str:
    payload_id = str(payload.get("id") or "").strip()
    payload_session_id = str(payload.get("session_id") or "").strip()
    if payload_id and (
        payload_id == payload_session_id
        or payload.get("parent_thread_id")
        or not payload_session_id
    ):
        return payload_id
    return payload_session_id or payload_id or fallback_thread_id


def read_session_id(path: Path, fallback_thread_id: str) -> str:
    with path.open("r", encoding="utf-8") as handle:
        first_line = handle.readline()
    if not first_line:
        return fallback_thread_id
    item = json.loads(first_line)
    if not isinstance(item, dict):
        return fallback_thread_id
    payload = item.get("payload")
    if item.get("type") != "session_meta" or not isinstance(payload, dict):
        return fallback_thread_id
    return session_id_from_payload(payload, fallback_thread_id)


def scan_local_catalog(paths: Paths) -> list[LocalThreadRecord]:
    with connect_db(paths.db_path, readonly=True) as conn:
        columns = get_thread_columns(conn)
        required = {"id", "rollout_path", "model_provider"}
        missing = required - columns
        if missing:
            raise RuntimeError(
                "Codex thread schema is missing upload fields: " + ", ".join(sorted(missing))
            )

        selected = [
            name
            for name in (
                "id",
                "rollout_path",
                "title",
                "source",
                "model_provider",
                "model",
                "cwd",
                "archived",
                "created_at",
                "updated_at",
                "created_at_ms",
                "updated_at_ms",
            )
            if name in columns
        ]
        rows = conn.execute(f"SELECT {', '.join(selected)} FROM threads ORDER BY id").fetchall()

    records: list[LocalThreadRecord] = []
    for row in rows:
        thread_id = str(row["id"])
        rollout_path = Path(str(row["rollout_path"]))
        if not rollout_path.is_file():
            raise RuntimeError(f"Session file does not exist: {rollout_path}")
        relative_path = relative_session_path(paths.codex_home, rollout_path)
        stat = rollout_path.stat()
        session = LocalSessionAsset(
            codex_thread_id=thread_id,
            codex_session_id=read_session_id(rollout_path, thread_id),
            relative_path=relative_path,
            path=rollout_path,
            file_size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        created_ms = row["created_at_ms"] if "created_at_ms" in selected else None
        updated_ms = row["updated_at_ms"] if "updated_at_ms" in selected else None
        records.append(
            LocalThreadRecord(
                codex_thread_id=thread_id,
                title=str(row["title"] or "") if "title" in selected else "",
                source=str(row["source"] or "") if "source" in selected else "",
                model_provider=str(row["model_provider"] or ""),
                model=str(row["model"]) if "model" in selected and row["model"] else None,
                original_cwd=str(row["cwd"]) if "cwd" in selected and row["cwd"] else None,
                archived=bool(row["archived"]) if "archived" in selected else False,
                codex_created_at=(
                    unix_timestamp_to_iso(created_ms, milliseconds=True)
                    if created_ms
                    else unix_timestamp_to_iso(row["created_at"])
                    if "created_at" in selected
                    else None
                ),
                codex_updated_at=(
                    unix_timestamp_to_iso(updated_ms, milliseconds=True)
                    if updated_ms
                    else unix_timestamp_to_iso(row["updated_at"])
                    if "updated_at" in selected
                    else None
                ),
                rollout_relative_path=relative_path,
                session=session,
            )
        )
    return records


def read_stable_file(
    path: Path,
    *,
    attempts: int = 3,
    delay_seconds: float = 0.1,
) -> StableFile:
    for attempt in range(attempts):
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        if before.st_size == after.st_size == len(content) and before.st_mtime_ns == after.st_mtime_ns:
            return StableFile(
                content=content,
                sha256=sha256_bytes(content),
                file_size=len(content),
                mtime_ns=after.st_mtime_ns,
            )
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    raise RuntimeError(f"Session file changed while it was being read: {path}")
