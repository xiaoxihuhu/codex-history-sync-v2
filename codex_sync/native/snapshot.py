from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .schema import CodexSchemaInspector, NativeSchemaReport

UTC = timezone.utc


@dataclass(frozen=True)
class NativeSnapshot:
    path: Path
    source_database: Path
    created_at: str
    integrity_check: str
    schema: NativeSchemaReport

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "source_database": str(self.source_database),
            "created_at": self.created_at,
            "integrity_check": self.integrity_check,
            "schema": self.schema.to_dict(),
        }


def _readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path.expanduser().resolve(strict=True).as_posix()}?mode=ro",
        uri=True,
    )


def create_native_snapshot(
    source_database: Path,
    destination: Path | None = None,
    *,
    label: str = "native",
) -> NativeSnapshot:
    """Create a consistent SQLite snapshot using SQLite's Backup API.

    This function is deliberately limited to local snapshots. It never uploads
    the resulting database and never replaces a target database during merge.
    """

    source = source_database.expanduser().resolve(strict=True)
    if destination is None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = source.parent / "history_sync_backups" / (
            f"native-snapshot-{label}-{timestamp}.sqlite"
        )
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )

    source_connection = _readonly_connection(source)
    target_connection = sqlite3.connect(str(temporary))
    try:
        source_connection.backup(target_connection, pages=256, sleep=0.05)
        target_connection.commit()
        integrity = str(
            target_connection.execute("PRAGMA integrity_check").fetchone()[0]
        )
    finally:
        source_connection.close()
        target_connection.close()

    if integrity.lower() != "ok":
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Native snapshot integrity check failed: {integrity}")
    os.replace(temporary, destination)
    schema = CodexSchemaInspector.inspect_path(destination)
    return NativeSnapshot(
        path=destination,
        source_database=source,
        created_at=datetime.now(tz=UTC).isoformat(),
        integrity_check=integrity,
        schema=schema,
    )
