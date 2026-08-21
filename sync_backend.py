"""Backward-compatible V1 entrypoint for the V2 local repair engine."""

from codex_sync.cli import main
from codex_sync.local.repair_engine import *  # noqa: F403


if __name__ == "__main__":
    raise SystemExit(main())
