from __future__ import annotations

import argparse
import json
import time

from codex_sync.local.repair_engine import (
    elapsed_ms,
    ensure_environment,
    get_status,
    make_backup,
    resolve_paths,
    restore_backup,
    sync_to_current_provider,
)


def to_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex history sync helper")
    parser.add_argument("--codex-home", help="Override Codex home directory")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")

    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status", help="Show current provider/thread status")
    status_parser.add_argument("--provider", help="Override current model_provider for display")
    status_parser.add_argument("--model", help="Override current model for display")
    sync_parser = subparsers.add_parser("sync", help="Move all thread providers to the current provider")
    sync_parser.add_argument("--provider", help="Target provider name to sync all threads to")
    sync_parser.add_argument("--model", help="Target model name to sync all threads to (omit to keep original)")
    restore_parser = subparsers.add_parser("restore", help="Restore from a backup")
    restore_parser.add_argument("--backup", help="Backup file path; newest backup is used when omitted")
    subparsers.add_parser("backup", help="Create a manual backup")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    paths = resolve_paths(args.codex_home)
    provider_override = getattr(args, "provider", None)
    model_override = getattr(args, "model", None)

    try:
        if args.command == "status":
            payload = get_status(paths, provider_override=provider_override, model_override=model_override)
        elif args.command == "sync":
            payload = sync_to_current_provider(
                paths,
                provider_override=provider_override,
                model_override=model_override,
            )
        elif args.command == "restore":
            payload = restore_backup(paths, args.backup)
        elif args.command == "backup":
            ensure_environment(paths)
            backup_started_at = time.monotonic()
            payload = {
                "action": "backup",
                "backup_path": str(make_backup(paths, "manual")),
                "timing": {"total_ms": elapsed_ms(backup_started_at)},
            }
        else:
            raise RuntimeError(f"Unsupported command: {args.command}")
    except Exception as exc:
        error_payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(to_json(error_payload))
        else:
            print(error_payload["error"])
        return 1

    payload["ok"] = True
    if args.json:
        print(to_json(payload))
    else:
        print(payload)
    return 0
