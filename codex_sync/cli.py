from __future__ import annotations

import argparse
import getpass
import json
import os
import time
from pathlib import Path

from codex_sync import __version__
from codex_sync.attachments import probe_attachments
from codex_sync.cloud import (
    AuthService,
    DeviceService,
    SupabaseAttachmentRepository,
    SupabaseClient,
    SupabaseDeviceRepository,
    SupabaseManualUploadRepository,
    SupabaseRestoreRepository,
    SupabaseSnapshotRepository,
    SupabaseWorkspaceRepository,
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
from codex_sync.progress import emit_progress
from codex_sync.sync import SyncStateStore
from codex_sync.sync.download import ManualDownloadEngine
from codex_sync.sync.download_attachments import AttachmentDownloadEngine
from codex_sync.sync.upload import ManualUploadEngine
from codex_sync.sync.upload_attachments import AttachmentUploadEngine
from codex_sync.workspace import WorkspaceManager
from codex_sync.versioning import CombinedSnapshotSource, SnapshotManager


def to_json(payload: dict[str, object]) -> str:
    if os.environ.get("CODEX_SYNC_JSON_LINES") == "1":
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
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
    subparsers.add_parser(
        "queue-status",
        help="Show local pending and completed upload queue items",
    )
    subparsers.add_parser("cloud-backup", help="Upload local Thread metadata and changed Session JSONL files")
    snapshot_create_parser = subparsers.add_parser(
        "cloud-snapshot-create",
        help="Create a manual cloud version Snapshot",
    )
    snapshot_create_parser.add_argument("--label")
    snapshot_create_parser.add_argument("--workspace-id")
    subparsers.add_parser("cloud-snapshot-list", help="List cloud version Snapshots")
    restore_cloud_parser = subparsers.add_parser(
        "cloud-restore",
        help="Restore missing plain-text Thread and Session history from Supabase",
    )
    restore_cloud_parser.add_argument("--thread-id", help="Restore only one Codex Thread ID")
    restore_cloud_parser.add_argument("--workspace-id", help="Restore only one logical Workspace")
    restore_cloud_parser.add_argument(
        "--target-cwd",
        help="Override the mapped working directory for restored Threads",
    )
    restore_cloud_parser.add_argument(
        "--replace-conflicting-sessions",
        action="store_true",
        help="Allow full recovery to replace same-path Sessions whose SHA256 differs",
    )
    snapshot_restore_parser = subparsers.add_parser(
        "cloud-snapshot-restore",
        help="Restore plain-text history from a selected cloud Snapshot",
    )
    snapshot_restore_parser.add_argument("--snapshot-id", required=True)
    snapshot_restore_parser.add_argument("--thread-id")
    snapshot_restore_parser.add_argument("--workspace-id")
    snapshot_restore_parser.add_argument("--target-cwd")
    snapshot_restore_parser.add_argument(
        "--replace-conflicting-sessions",
        action="store_true",
        help="Allow full recovery to replace same-path Sessions whose SHA256 differs",
    )
    subparsers.add_parser(
        "workspace-list",
        help="List cloud Workspaces and this device's local mappings",
    )
    workspace_map_parser = subparsers.add_parser(
        "workspace-map",
        help="Map a cloud Workspace to a local working directory",
    )
    workspace_map_parser.add_argument("--workspace-id", required=True)
    workspace_map_parser.add_argument("--path", required=True)
    subparsers.add_parser(
        "cloud-upload-attachments",
        help="Upload local images and attachments by SHA256 after Thread/Session backup",
    )
    subparsers.add_parser(
        "cloud-restore-attachments",
        help="Download attachments and rewrite restored Session paths",
    )
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
            "queue-status",
            "cloud-backup",
            "cloud-snapshot-create",
            "cloud-snapshot-list",
            "cloud-snapshot-restore",
            "cloud-restore",
            "workspace-list",
            "workspace-map",
            "cloud-upload-attachments",
            "cloud-restore-attachments",
        }:
            app_paths = default_app_paths()
            state = SyncStateStore(app_paths)
            if args.command == "device-info":
                payload = {
                    "action": "device-info",
                    "device": state.get_or_create_device(__version__).to_dict(),
                }
            elif args.command == "queue-status":
                payload = {
                    "action": "queue-status",
                    "queue": [item.to_dict() for item in state.list_upload_queue()],
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
                        WorkspaceManager(
                            auth,
                            devices,
                            state,
                            SupabaseWorkspaceRepository(client),
                        ),
                        state,
                    ).upload()
                    snapshot = SnapshotManager(
                        auth,
                        devices,
                        SupabaseSnapshotRepository(client),
                        CombinedSnapshotSource(
                            SupabaseRestoreRepository(client),
                            SupabaseAttachmentRepository(client),
                        ),
                        SupabaseRestoreRepository(client),
                    ).create(
                        snapshot_type="automatic",
                        label="cloud-backup",
                    )
                    payload = {
                        "action": "cloud-backup",
                        "summary": summary.to_dict(),
                        "snapshot": snapshot,
                    }
                elif args.command == "cloud-snapshot-create":
                    snapshot = SnapshotManager(
                        auth,
                        devices,
                        SupabaseSnapshotRepository(client),
                        CombinedSnapshotSource(
                            SupabaseRestoreRepository(client),
                            SupabaseAttachmentRepository(client),
                        ),
                        SupabaseRestoreRepository(client),
                    ).create(
                        snapshot_type="manual",
                        label=args.label,
                        workspace_id=args.workspace_id,
                    )
                    payload = {
                        "action": "cloud-snapshot-create",
                        "snapshot": snapshot,
                    }
                elif args.command == "cloud-snapshot-list":
                    snapshots = SnapshotManager(
                        auth,
                        devices,
                        SupabaseSnapshotRepository(client),
                        CombinedSnapshotSource(
                            SupabaseRestoreRepository(client),
                            SupabaseAttachmentRepository(client),
                        ),
                        SupabaseRestoreRepository(client),
                    ).list()
                    payload = {
                        "action": "cloud-snapshot-list",
                        "snapshots": snapshots,
                    }
                elif args.command == "cloud-restore":
                    summary = ManualDownloadEngine(
                        paths,
                        auth,
                        devices,
                        SupabaseRestoreRepository(client),
                        WorkspaceManager(
                            auth,
                            devices,
                            state,
                            SupabaseWorkspaceRepository(client),
                        ),
                    ).restore(
                        codex_thread_id=args.thread_id,
                        workspace_id=args.workspace_id,
                        target_cwd=Path(args.target_cwd) if args.target_cwd else None,
                        replace_conflicting_sessions=args.replace_conflicting_sessions,
                    )
                    payload = {
                        "action": "cloud-restore",
                        "summary": summary.to_dict(),
                    }
                elif args.command == "cloud-snapshot-restore":
                    snapshot_manager = SnapshotManager(
                        auth,
                        devices,
                        SupabaseSnapshotRepository(client),
                        CombinedSnapshotSource(
                            SupabaseRestoreRepository(client),
                            SupabaseAttachmentRepository(client),
                        ),
                        SupabaseRestoreRepository(client),
                    )
                    summary = ManualDownloadEngine(
                        paths,
                        auth,
                        devices,
                        snapshot_manager.load_restore_repository(args.snapshot_id),
                        WorkspaceManager(
                            auth,
                            devices,
                            state,
                            SupabaseWorkspaceRepository(client),
                        ),
                    ).restore(
                        codex_thread_id=args.thread_id,
                        workspace_id=args.workspace_id,
                        target_cwd=Path(args.target_cwd) if args.target_cwd else None,
                        replace_conflicting_sessions=args.replace_conflicting_sessions,
                    )
                    payload = {
                        "action": "cloud-snapshot-restore",
                        "snapshot_id": args.snapshot_id,
                        "summary": summary.to_dict(),
                    }
                elif args.command == "workspace-list":
                    manager = WorkspaceManager(
                        auth,
                        devices,
                        state,
                        SupabaseWorkspaceRepository(client),
                    )
                    payload = {
                        "action": "workspace-list",
                        "workspaces": manager.list_workspaces(),
                    }
                elif args.command == "workspace-map":
                    manager = WorkspaceManager(
                        auth,
                        devices,
                        state,
                        SupabaseWorkspaceRepository(client),
                    )
                    payload = {
                        "action": "workspace-map",
                        "mapping": manager.map_workspace(
                            args.workspace_id,
                            Path(args.path),
                        ),
                    }
                elif args.command == "cloud-upload-attachments":
                    summary = AttachmentUploadEngine(
                        paths,
                        auth,
                        devices,
                        SupabaseAttachmentRepository(client),
                    ).upload()
                    payload = {
                        "action": "cloud-upload-attachments",
                        "summary": summary.to_dict(),
                    }
                elif args.command == "cloud-restore-attachments":
                    summary = AttachmentDownloadEngine(
                        paths,
                        auth,
                        devices,
                        SupabaseAttachmentRepository(client),
                    ).restore()
                    payload = {
                        "action": "cloud-restore-attachments",
                        "summary": summary.to_dict(),
                    }
                else:
                    raise RuntimeError(f"Unsupported cloud command: {args.command}")
        else:
            raise RuntimeError(f"Unsupported command: {args.command}")
    except Exception as exc:
        error_payload = {"ok": False, "error": str(exc)}
        emit_progress("complete", ok=False, error=str(exc))
        if args.json:
            print(to_json(error_payload))
        else:
            print(error_payload["error"])
        return 1

    payload["ok"] = True
    emit_progress("complete", ok=True, task=args.command)
    if args.json:
        print(to_json(payload))
    else:
        print(payload)
    return 0
