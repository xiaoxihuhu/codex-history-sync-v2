from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess, QTimer
from PySide6.QtWidgets import QApplication, QLineEdit

from codex_sync.config import default_app_paths, load_supabase_config
from codex_sync.gui import HistorySyncWindow, execute_cli
from launch_gui import _internal_cli_main, _internal_command_name


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.environment_patch = mock.patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": self.temp_dir.name,
                "QT_QPA_PLATFORM": "offscreen",
            },
        )
        self.environment_patch.start()
        self.previous_url = os.environ.pop("CODEX_SYNC_SUPABASE_URL", None)
        self.previous_key = os.environ.pop("CODEX_SYNC_SUPABASE_KEY", None)
        self.windows: list[HistorySyncWindow] = []

    def tearDown(self) -> None:
        for window in self.windows:
            window.close()
        self.app.processEvents()
        if self.previous_url is not None:
            os.environ["CODEX_SYNC_SUPABASE_URL"] = self.previous_url
        if self.previous_key is not None:
            os.environ["CODEX_SYNC_SUPABASE_KEY"] = self.previous_key
        self.environment_patch.stop()
        self.temp_dir.cleanup()

    def window(
        self,
        responses: dict[str, tuple[int, dict[str, object]]] | None = None,
    ) -> tuple[HistorySyncWindow, list[dict[str, object]]]:
        calls: list[dict[str, object]] = []
        scripted = responses or {}

        def runner(
            arguments: list[str],
            *,
            secret_inputs: list[str] | None = None,
            environment: dict[str, str] | None = None,
        ) -> tuple[int, dict[str, object]]:
            calls.append(
                {
                    "arguments": list(arguments),
                    "secret_inputs": list(secret_inputs or []),
                    "environment": dict(environment or {}),
                }
            )
            return scripted.get(
                arguments[0],
                (0, {"ok": True, "action": arguments[0]}),
            )

        window = HistorySyncWindow(command_runner=runner, auto_refresh=False)
        self.windows.append(window)

        def run_sync(
            arguments: list[str],
            callback: object,
            *,
            secret_inputs: list[str] | None = None,
            environment: dict[str, str] | None = None,
        ) -> None:
            code, payload = runner(
                arguments,
                secret_inputs=secret_inputs,
                environment=environment,
            )
            callback(code, payload)

        def sequence_sync(commands: list[list[str]], callback: object) -> None:
            for arguments in commands:
                code, payload = runner(arguments)
                if code != 0 or not payload.get("ok"):
                    callback(code or 1, payload)
                    return
            callback(0, {"ok": True})

        window._run = run_sync  # type: ignore[method-assign]
        window._run_sequence = sequence_sync  # type: ignore[method-assign]
        return window, calls

    def test_unconfigured_startup_is_nonfatal_and_shows_start_configuration(self) -> None:
        window, _calls = self.window()
        window._apply_startup(
            {
                "status": (
                    0,
                    {
                        "ok": True,
                        "codex_home": r"C:\Empty\.codex",
                        "total_threads": 0,
                        "session_file_count": 0,
                        "indexed_threads": 0,
                    },
                ),
                "auth": (
                    1,
                    {
                        "ok": False,
                        "error": "Supabase is not configured",
                    },
                ),
                "device_info": (
                    0,
                    {
                        "ok": True,
                        "device": {"device_name": "NewComputer"},
                    },
                ),
            }
        )
        self.assertEqual(window.account_label.text(), "账号: 云端未配置")
        self.assertFalse(window.start_config_button.isHidden())
        self.assertIn("云端尚未配置", window.log.toPlainText())

    def test_execute_cli_saves_valid_configuration_without_key_in_arguments(self) -> None:
        key = "sb_publishable_gui_test_key_123456789"
        code, payload = execute_cli(
            ["cloud-configure", "--url", "https://example.supabase.co"],
            secret_inputs=[key],
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        config = load_supabase_config(default_app_paths())
        self.assertEqual(config.project_url, "https://example.supabase.co")
        self.assertEqual(config.public_key, key)

    def test_gui_rejects_service_role_and_secret_keys(self) -> None:
        window, calls = self.window()
        window.project_url_edit.setText("https://example.supabase.co")
        window.publishable_key_edit.setText("service_role_not_allowed")
        with mock.patch("codex_sync.gui.QMessageBox.warning") as warning:
            window.save_cloud_configuration()
        self.assertEqual(calls, [])
        warning.assert_called_once()

    def test_key_and_password_fields_use_password_echo_mode(self) -> None:
        window, _calls = self.window()
        self.assertEqual(
            window.publishable_key_edit.echoMode(),
            QLineEdit.EchoMode.Password,
        )
        self.assertEqual(
            window.password_edit.echoMode(),
            QLineEdit.EchoMode.Password,
        )

    def test_login_uses_secret_input_and_never_places_password_in_arguments(self) -> None:
        window, calls = self.window(
            {
                "auth-sign-in": (
                    0,
                    {
                        "ok": True,
                        "session": {"user": {"email": "user@example.com"}},
                    },
                )
            }
        )
        window.refresh_overview = mock.Mock()  # type: ignore[method-assign]
        window.email_edit.setText("user@example.com")
        window.password_edit.setText("private-password")
        window.sign_in()
        self.assertEqual(
            calls[-1]["arguments"],
            ["auth-sign-in", "--email", "user@example.com"],
        )
        self.assertEqual(calls[-1]["secret_inputs"], ["private-password"])
        self.assertNotIn("private-password", " ".join(calls[-1]["arguments"]))
        self.assertEqual(window.password_edit.text(), "")
        window.refresh_overview.assert_called_once()

    def test_login_failure_is_human_readable_and_password_is_not_logged(self) -> None:
        password = "private-password"
        window, _calls = self.window(
            {
                "auth-sign-in": (
                    1,
                    {
                        "ok": False,
                        "error": f"Invalid login credentials: {password}",
                    },
                )
            }
        )
        window.email_edit.setText("user@example.com")
        window.password_edit.setText(password)
        with mock.patch("codex_sync.gui.QMessageBox.critical") as critical:
            window.sign_in()
        self.assertNotIn(password, window.log.toPlainText())
        self.assertIn("[REDACTED]", window.log.toPlainText())
        self.assertIn("登录失败", critical.call_args.args[2])

    def test_logout_clears_displayed_account_state(self) -> None:
        window, calls = self.window(
            {
                "auth-sign-out": (
                    0,
                    {
                        "ok": True,
                        "remote_signed_out": True,
                        "local_session_removed": True,
                    },
                )
            }
        )
        window._set_configured_state(True)
        window._set_auth_state({"email": "user@example.com"})
        window.sign_out()
        self.assertEqual(calls[-1]["arguments"], ["auth-sign-out"])
        self.assertEqual(window.account_label.text(), "账号: 未登录")
        self.assertFalse(window.logout_button.isEnabled())

    def test_workspace_row_mapping_passes_hidden_workspace_id_to_cli(self) -> None:
        window, calls = self.window()
        window.refresh_workspaces = mock.Mock()  # type: ignore[method-assign]
        workspace_name = "shopyy���"
        window._apply_workspaces(
            {
                "workspaces": [
                    {
                        "workspace_id": "workspace-id-1",
                        "name": workspace_name,
                        "cloud_device_path": r"C:\Source\shop-tool",
                        "mapped": False,
                    }
                ]
            }
        )
        self.assertEqual(window.workspace_table.item(0, 0).text(), workspace_name)
        target = Path(self.temp_dir.name) / "shop-tool"
        target.mkdir()
        window.map_workspace_row(0, target)
        self.assertEqual(
            calls[-1]["arguments"],
            [
                "workspace-map",
                "--workspace-id",
                "workspace-id-1",
                "--path",
                str(target),
            ],
        )

    def test_internal_cli_forces_utf8_stdout_for_replacement_characters(self) -> None:
        from codex_sync import cli

        output_bytes = io.BytesIO()
        output = io.TextIOWrapper(output_bytes, encoding="gbk", newline="")
        original_json_lines = os.environ.get("CODEX_SYNC_JSON_LINES")

        def fake_main() -> int:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "action": "workspace-list",
                        "workspaces": [{"name": "shopyy���"}],
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        try:
            with (
                mock.patch("launch_gui.sys.stdout", output),
                mock.patch(
                    "launch_gui.sys.argv",
                    ["launch_gui.py", "--internal-cli", "--json", "workspace-list"],
                ),
                mock.patch("codex_sync.cli.main", side_effect=fake_main),
            ):
                self.assertEqual(_internal_cli_main(), 0)
            output.flush()
            lines = [
                json.loads(line)
                for line in output_bytes.getvalue().decode("utf-8").splitlines()
                if line
            ]
        finally:
            output.detach()
            if original_json_lines is None:
                os.environ.pop("CODEX_SYNC_JSON_LINES", None)
            else:
                os.environ["CODEX_SYNC_JSON_LINES"] = original_json_lines

        result = next(item for item in lines if item.get("ok") is True)
        self.assertEqual(result["workspaces"][0]["name"], "shopyy���")

    def test_frozen_internal_cli_exits_after_emitting_result(self) -> None:
        import launch_gui

        with (
            mock.patch(
                "launch_gui.sys.argv",
                ["CodexHistorySync.exe", "--internal-cli", "--json", "queue-status"],
            ),
            mock.patch("launch_gui._internal_cli_main", return_value=0),
            mock.patch.object(launch_gui.sys, "frozen", True, create=True),
            mock.patch("launch_gui.os._exit") as exit_process,
        ):
            launch_gui.main()

        exit_process.assert_called_once_with(0)

    def test_bulk_workspace_mapping_creates_safe_subdirectories(self) -> None:
        window, calls = self.window()
        window.refresh_workspaces = mock.Mock()  # type: ignore[method-assign]
        window._workspace_rows = [
            {"workspace_id": "one", "name": "shopyy���"},
            {"workspace_id": "two", "name": "shopyy���"},
        ]
        root = Path(self.temp_dir.name) / "Recovered"
        window.map_all_workspaces_to(root)
        mapped_commands = [
            call["arguments"]
            for call in calls
            if call["arguments"][0] == "workspace-map"
        ]
        self.assertEqual(len(mapped_commands), 2)
        paths = [Path(command[-1]) for command in mapped_commands]
        self.assertTrue(all(path.is_dir() for path in paths))
        self.assertNotEqual(paths[0].name.casefold(), paths[1].name.casefold())
        self.assertTrue(paths[0].name.startswith("shopyy���"))
        self.assertTrue(paths[1].name.startswith("shopyy���"))

    def test_original_backup_and_restore_buttons_keep_original_commands(self) -> None:
        window, calls = self.window()
        window.refresh_overview = mock.Mock()  # type: ignore[method-assign]
        window.refresh_snapshots = mock.Mock()  # type: ignore[method-assign]
        window.local_backup()
        window.cloud_backup()
        window.cloud_restore()
        commands = [call["arguments"][0] for call in calls]
        self.assertEqual(commands, ["backup", "cloud-backup", "cloud-restore"])
        self.assertNotIn("cloud-upload-attachments", commands)

    def test_default_gui_runs_internal_cli_in_qprocess_without_blocking_events(self) -> None:
        window = HistorySyncWindow(auto_refresh=False)
        self.windows.append(window)
        completed: list[tuple[int, object]] = []
        heartbeats = 0
        timer = QTimer()
        timer.setInterval(5)

        def heartbeat() -> None:
            nonlocal heartbeats
            heartbeats += 1

        timer.timeout.connect(heartbeat)
        timer.start()
        started_at = time.perf_counter()
        started = window._task_controller.start(
            "云端备份",
            ["device-info"],
            lambda code, payload: completed.append((code, payload)),
            button=window.cloud_backup_button,
        )
        feedback_ms = (time.perf_counter() - started_at) * 1000

        self.assertTrue(started)
        self.assertIsInstance(window._task_controller.process, QProcess)
        self.assertEqual(window.cloud_backup_button.text(), "云端备份中...")
        self.assertFalse(window.cloud_backup_button.isEnabled())
        self.assertLess(feedback_ms, 100)

        deadline = time.monotonic() + 10
        while window._task_controller.running and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        timer.stop()

        self.assertFalse(window._task_controller.running)
        self.assertTrue(completed)
        self.assertEqual(completed[0][0], 0)
        self.assertTrue(completed[0][1]["ok"])
        self.assertGreater(heartbeats, 0)
        self.assertEqual(window.cloud_backup_button.text(), "云端备份")
        self.assertTrue(window.cloud_backup_button.isEnabled())
        self.assertFalse(hasattr(window, "_threads"))

    def test_internal_cli_progress_finds_command_after_global_option_value(self) -> None:
        from codex_sync import cli

        self.assertEqual(
            _internal_command_name(
                cli,
                ["--json", "--codex-home", "C:/isolated/home", "cloud-backup"],
            ),
            "cloud-backup",
        )

    def test_execute_cli_serializes_process_global_argv_and_stdout(self) -> None:
        original_argv = list(sys.argv)
        observed_threads: set[int] = set()

        def fake_main() -> int:
            command = sys.argv[-1]
            observed_threads.add(threading.get_ident())
            time.sleep(0.02)
            print(json.dumps({"ok": True, "command": command}))
            return 0

        with mock.patch("codex_sync.cli.main", side_effect=fake_main):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(execute_cli, [f"command-{index}"])
                    for index in range(4)
                ]
                results = [future.result() for future in futures]

        self.assertEqual(list(sys.argv), original_argv)
        self.assertEqual(
            [payload["command"] for _code, payload in results],
            [f"command-{index}" for index in range(4)],
        )
        self.assertGreater(len(observed_threads), 1)


if __name__ == "__main__":
    unittest.main()
