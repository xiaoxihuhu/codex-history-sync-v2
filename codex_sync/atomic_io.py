from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Callable


def atomic_write_stream(path: Path, writer: Callable[[BinaryIO], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    atomic_write_stream(path, lambda handle: handle.write(content))


def atomic_copy_file(source: Path, destination: Path) -> None:
    def copy(output: BinaryIO) -> None:
        with source.open("rb") as input_handle:
            shutil.copyfileobj(input_handle, output, length=1024 * 1024)

    atomic_write_stream(destination, copy)


def atomic_write_json(path: Path, payload: Any) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    atomic_write_bytes(path, content)
