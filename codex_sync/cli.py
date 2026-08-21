from __future__ import annotations

import argparse
import getpass
import json
import time

from codex_sync import __version__
from codex_sync.attachments import probe_attachments
from codex_sync.cloud import (
    AuthService,
    DeviceService,
    SupabaseClient,
    SupabaseDeviceRepository,
    SupabaseManualUploadRepository,
)
from codex_sync.config import (
    SupabaseConfig,
    default_app_paths,
    load_supabase_config,
    save_supabase_config,
)
from codex_sync.local.repair_engine import (
    elapsed_ms,
    ensure_environment,
    get_status,
    make_backup,
    resolve_paths,
    restore_backup,
    sync_to_current_provider,
)
from codex_sync.sync import SyncStateStore
from codex_sync.sync.upload import ManualUploadEngine


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
    probe_parser = subparsers.add_parser(
        "probe-attachments",
        help="Inspect local attachment and image references without modifying Codex data",
    )
    probe_parser.add_argument(
        "--active-only",
        action="store_true",
        help="Skip archived session files",
    )
    configure_parser = subparsers.add_parser(
        "cloud-configure",
        help="Save the Supabase project URL and client-safe publishable/anon key",
    )
    configure_parser.add_argument("--url", required=True, help="Supabase project URL")
    signup_parser = subparsers.add_parser("auth-sign-up", help="Create a Codex Sync account")
    signup_parser.add_argument("--email", required=True)
    signin_parser = subparsers.add_parser("auth-sign-in", help="Sign in to Codex Sync")
    signin_parser.add_argument("--email", required=True)
    subparsers.add_parser("auth-status", help="Restore login and show the current Codex Sync account")
    subparsers.add_parser("auth-sign-out", help="Sign out and remove the local encrypted Auth session")
    subparsers.add_parser("device-info", help="Show this installation's persistent device identity")
    subparsers.add_parser("device-register", help="Register or refresh this device in Supabase")
    subparsers.add_parser("device-list", help="List devices for the current Codex Sync account")
    subparsers.add_parser("cloud-backup", help="Upload local Thread metadata and changed Session JSONL files")
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
        elif args.command == "probe-attachments":
            payload = probe_attachments(
                paths.codex_home,
                include_archived=not args.active_only,
            ).to_dict()
        elif args.command == "cloud-configure":
            public_key = getpass.getpass("Supabase publishable/anon key: ")
            app_paths = default_app_paths()
            config_path = save_supabase_config(
                SupabaseConfig(args.url, public_key),
                app_paths,
            )
            payload = {
                "action": "cloud-configure",
                "config_path": str(config_path),
                "project_url": args.url.rstrip("/"),
            }
        elif args.command in {
            "auth-sign-up",
            "auth-sign-in",
            "auth-status",
            "auth-sign-out",
            "device-info",
            "device-register",
            "device-list",
            "cloud-backup",
        }:
            app_paths = default_app_paths()
            state = SyncStateStore(app_paths)
            if args.command == "device-info":
                payload = {
                    "action": "device-info",
                    "device": state.get_or_create_device(__version__).to_dict(),
                }
            else:
                config = load_supabase_config(app_paths)
                client = SupabaseClient(config)
                auth = AuthService(client, state)
                devices = DeviceService(auth, state, SupabaseDeviceRepository(client))
                if args.command == "auth-sign-up":
                    password = getpass.getpass("Codex Sync password: ")
                    result = auth.sign_up(args.email, password)
                    payload = {"action": "auth-sign-up", **result.public_dict()}
                elif args.command == "auth-sign-in":
                    password = getpass.getpass("Codex Sync password: ")
                    session = auth.sign_in(args.email, password)
                    payload = {"action": "auth-sign-in", "session": session.public_dict()}
                elif args.command == "auth-status":
                    account = auth.current_account()
                    payload = {
                        "action": "auth-status",
                        "signed_in": account is not None,
                        "account": account.to_dict() if account else None,
                    }
                elif args.command == "auth-sign-out":
                    payload = {
                        "action": "auth-sign-out",
                        "remote_signed_out": auth.sign_out(),
                        "local_session_removed": True,
                    }
                elif args.command == "device-register":
                    payload = {
                        "action": "device-register",
                        "device": devices.register_current_device(),
                    }
                elif args.command == "device-list":
                    payload = {
                        "action": "device-list",
                        "devices": devices.list_devices(),
                    }
                elif args.command == "cloud-backup":
                    summary = ManualUploadEngine(
                        paths,
                        auth,
                        devices,
                        SupabaseManualUploadRepository(client),
                    ).upload()
                    payload = {
                        "action": "cloud-backup",
                        "summary": summary.to_dict(),
                    }
                else:
                    raise RuntimeError(f"Unsupported cloud command: {args.command}")
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
