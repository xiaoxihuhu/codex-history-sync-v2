from __future__ import annotations

from collections.abc import Callable
from typing import Any


ProgressSink = Callable[[dict[str, Any]], None]

_progress_sink: ProgressSink | None = None


def set_progress_sink(sink: ProgressSink | None) -> None:
    global _progress_sink
    _progress_sink = sink


def emit_progress(event: str, **payload: Any) -> None:
    if _progress_sink is None:
        return
    _progress_sink({"event": event, **payload})
