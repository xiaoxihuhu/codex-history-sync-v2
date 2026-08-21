from __future__ import annotations

import unittest

import sync_backend
from codex_sync import __version__
from codex_sync.cli import build_parser
from codex_sync.local import repair_engine


class CompatibilityTests(unittest.TestCase):
    def test_v1_entrypoint_exports_local_repair_engine(self) -> None:
        self.assertIs(sync_backend.get_status, repair_engine.get_status)
        self.assertIs(sync_backend.resolve_paths, repair_engine.resolve_paths)
        self.assertIs(sync_backend.restore_backup, repair_engine.restore_backup)
        self.assertIs(sync_backend.sync_to_current_provider, repair_engine.sync_to_current_provider)

    def test_cli_keeps_v1_commands(self) -> None:
        parser = build_parser()

        for command in (
            "status",
            "sync",
            "restore",
            "backup",
            "probe-attachments",
            "cloud-configure",
            "auth-sign-up",
            "auth-sign-in",
            "auth-status",
            "auth-sign-out",
            "device-info",
            "device-register",
            "device-list",
            "cloud-backup",
            "cloud-restore",
            "cloud-upload-attachments",
        ):
            command_args = [command]
            if command == "cloud-configure":
                command_args += ["--url", "https://example.supabase.co"]
            elif command in {"auth-sign-up", "auth-sign-in"}:
                command_args += ["--email", "user@example.com"]
            args = parser.parse_args(command_args)
            self.assertEqual(args.command, command)

    def test_v2_package_has_development_version(self) -> None:
        self.assertEqual(__version__, "2.0.0.dev0")


if __name__ == "__main__":
    unittest.main()
