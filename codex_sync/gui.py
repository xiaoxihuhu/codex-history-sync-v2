from __future__ import annotations

import io
import json
import os
import sys
import threading
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable, Mapping

from codex_sync.config import (
    SUPABASE_KEY_ENV,
    SUPABASE_URL_ENV,
    SupabaseConfig,
    default_app_paths,
    is_forbidden_client_key,
    load_supabase_config,
)
from codex_sync.gui_flow import (
    RECOVERY_STEPS,
    GuiFlowError,
    build_bulk_mapping_targets,
    build_recovery_report,
    human_error,
    is_unconfigured_error,
    run_full_restore,
    should_suggest_new_device,
)


_CLI_LOCK = threading.Lock()


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {key: _redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, secrets) for item in value)
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    return value


def execute_cli(
    arguments: list[str],
    *,
    secret_inputs: list[str] | tuple[str, ...] | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run the JSON CLI in-process so the same code works from source and EXE."""
    from codex_sync import cli

    secrets = tuple(str(value) for value in (secret_inputs or ()))
    environment_overrides = dict(environment or {})
    with _CLI_LOCK:
        previous_argv = sys.argv
        previous_getpass = cli.getpass.getpass
        previous_environment = {
            key: os.environ.get(key) for key in environment_overrides
        }
        secret_iterator = iter(secrets)
        output = io.StringIO()
        exit_code = 1
        try:
            sys.argv = ["codex-history-sync", "--json", *arguments]

            def next_secret(_prompt: str = "") -> str:
                try:
                    return next(secret_iterator)
                except StopIteration as exc:
                    raise RuntimeError(
                        "GUI did not provide a required secret input"
                    ) from exc

            cli.getpass.getpass = next_secret
            for key, value in environment_overrides.items():
                os.environ[key] = value
            with redirect_stdout(output):
                exit_code = cli.main()
        finally:
            sys.argv = previous_argv
            cli.getpass.getpass = previous_getpass
            for key, value in previous_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    text = output.getvalue().strip()
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    if not text:
        return exit_code, {"ok": False, "error": "CLI returned no output"}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return exit_code, {"ok": False, "error": text}
    if not isinstance(payload, dict):
        return exit_code, {"ok": False, "error": "CLI returned an invalid JSON object"}
    return exit_code, _redact_value(payload, secrets)


try:
    from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QFileDialog,
        QFormLayout,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )

    PYSIDE6_AVAILABLE = True
except ImportError:
    PYSIDE6_AVAILABLE = False


if PYSIDE6_AVAILABLE:

    Runner = Callable[..., tuple[int, dict[str, Any]]]

    class CommandWorker(QObject):
        finished = Signal(int, object)

        def __init__(
            self,
            runner: Runner,
            arguments: list[str],
            *,
            secret_inputs: list[str] | None = None,
            environment: Mapping[str, str] | None = None,
        ) -> None:
            super().__init__()
            self.runner = runner
            self.arguments = arguments
            self.secret_inputs = secret_inputs or []
            self.environment = dict(environment or {})

        def run(self) -> None:
            try:
                code, payload = self.runner(
                    self.arguments,
                    secret_inputs=self.secret_inputs,
                    environment=self.environment,
                )
            except Exception as exc:
                code, payload = 1, {"ok": False, "error": str(exc)}
            finally:
                self.secret_inputs.clear()
            self.finished.emit(code, payload)


    class CommandSequenceWorker(QObject):
        finished = Signal(int, object)

        def __init__(self, runner: Runner, commands: list[list[str]]) -> None:
            super().__init__()
            self.runner = runner
            self.commands = commands

        def run(self) -> None:
            results: list[dict[str, Any]] = []
            try:
                for arguments in self.commands:
                    code, payload = self.runner(arguments)
                    data = payload if isinstance(payload, dict) else {}
                    if code != 0 or not data.get("ok"):
                        self.finished.emit(
                            1,
                            {
                                "ok": False,
                                "error": str(
                                    data.get("error") or f"{arguments[0]} failed"
                                ),
                                "failed_command": arguments[0],
                                "results": results,
                            },
                        )
                        return
                    results.append(data)
            except Exception as exc:
                self.finished.emit(1, {"ok": False, "error": str(exc)})
                return
            self.finished.emit(0, {"ok": True, "results": results})


    class StartupWorker(QObject):
        finished = Signal(object)

        def __init__(self, runner: Runner) -> None:
            super().__init__()
            self.runner = runner

        def run(self) -> None:
            results: dict[str, tuple[int, dict[str, Any]]] = {}
            try:
                results["status"] = self.runner(["status"])
                results["auth"] = self.runner(["auth-status"])
                results["device_info"] = self.runner(["device-info"])

                auth_code, auth_payload = results["auth"]
                if (
                    auth_code == 0
                    and isinstance(auth_payload, dict)
                    and auth_payload.get("ok")
                    and auth_payload.get("signed_in")
                ):
                    for key, command in (
                        ("device_register", ["device-register"]),
                        ("devices", ["device-list"]),
                        ("workspaces", ["workspace-list"]),
                        ("snapshots", ["cloud-snapshot-list"]),
                    ):
                        results[key] = self.runner(command)
            except Exception as exc:
                results["worker_error"] = (1, {"ok": False, "error": str(exc)})
            self.finished.emit(results)


    class RecoveryWorker(QObject):
        progress = Signal(str, str, str)
        finished = Signal(int, object)

        def __init__(self, runner: Runner) -> None:
            super().__init__()
            self.runner = runner

        def run(self) -> None:
            try:
                result = run_full_restore(
                    lambda arguments: self.runner(arguments),
                    lambda step, status, detail: self.progress.emit(
                        step,
                        status,
                        detail,
                    ),
                )
            except GuiFlowError as exc:
                self.finished.emit(
                    1,
                    {
                        "ok": False,
                        "error": str(exc),
                        "destination": exc.destination,
                    },
                )
            except Exception as exc:
                self.finished.emit(1, {"ok": False, "error": str(exc)})
            else:
                self.finished.emit(0, {"ok": True, **result.to_dict()})


    class HistorySyncWindow(QMainWindow):
        def __init__(
            self,
            *,
            command_runner: Runner = execute_cli,
            auto_refresh: bool = True,
        ) -> None:
            super().__init__()
            self.setWindowTitle("Codex History Sync")
            self.resize(1180, 820)
            self._runner = command_runner
            self._threads: list[QThread] = []
            self._workers: list[QObject] = []
            self._workspace_rows: list[dict[str, Any]] = []
            self._snapshot_rows: list[dict[str, Any]] = []
            self._last_status: dict[str, Any] = {}
            self._last_device_info: dict[str, Any] = {}
            self._last_devices: list[dict[str, Any]] = []
            self._cloud_configured = False
            self._signed_in = False
            self._build_ui()
            self._load_configuration()
            self._reset_recovery_steps()
            if auto_refresh:
                QTimer.singleShot(0, self.refresh_overview)

        def _build_ui(self) -> None:
            root = QWidget()
            root_layout = QVBoxLayout(root)
            self.tabs = QTabWidget()
            root_layout.addWidget(self.tabs, 1)
            self.log = QTextEdit()
            self.log.setReadOnly(True)
            self.log.setPlaceholderText("任务日志")
            self.log.setMaximumHeight(180)
            root_layout.addWidget(self.log)
            self.setCentralWidget(root)

            self._build_overview_tab()
            self._build_settings_tab()
            self._build_workspace_tab()
            self._build_recovery_tab()
            self._build_queue_tab()
            self._build_snapshot_tab()

        def _build_overview_tab(self) -> None:
            overview = QWidget()
            layout = QVBoxLayout(overview)

            status_box = QGroupBox("连接状态")
            status_grid = QGridLayout(status_box)
            self.local_label = QLabel("本地 Codex: 未检测")
            self.cloud_config_label = QLabel("云端配置: 未读取")
            self.account_label = QLabel("账号: 未读取")
            self.cloud_connection_label = QLabel("云端连接: 未读取")
            self.device_label = QLabel("设备: 未读取")
            self.counts_label = QLabel("Thread / Session: 未读取")
            self.repair_label = QLabel("待修复: 未读取")
            labels = (
                self.local_label,
                self.cloud_config_label,
                self.account_label,
                self.cloud_connection_label,
                self.device_label,
                self.counts_label,
                self.repair_label,
            )
            for index, label in enumerate(labels):
                status_grid.addWidget(label, index // 2, index % 2)
            layout.addWidget(status_box)

            self.new_device_box = QGroupBox("新电脑恢复")
            new_device_layout = QGridLayout(self.new_device_box)
            new_device_layout.addWidget(
                QLabel(
                    "检测到这可能是一台新电脑。云端已有可恢复历史。\n"
                    "建议先完成 Workspace 映射，然后使用“完整恢复到本机”。"
                ),
                0,
                0,
                1,
                2,
            )
            self.guided_restore_button = QPushButton("开始恢复")
            self.guided_restore_button.clicked.connect(self.begin_guided_restore)
            later_button = QPushButton("稍后")
            later_button.clicked.connect(lambda: self.new_device_box.setVisible(False))
            new_device_layout.addWidget(self.guided_restore_button, 1, 0)
            new_device_layout.addWidget(later_button, 1, 1)
            self.new_device_box.setVisible(False)
            layout.addWidget(self.new_device_box)

            action_box = QGroupBox("本地与云端操作")
            action_layout = QGridLayout(action_box)
            self.refresh_button = QPushButton("刷新状态")
            self.refresh_button.clicked.connect(self.refresh_overview)
            self.repair_button = QPushButton("本地修复")
            self.repair_button.clicked.connect(self.repair_local)
            self.local_backup_button = QPushButton("本地备份")
            self.local_backup_button.clicked.connect(self.local_backup)
            self.cloud_backup_button = QPushButton("云端备份")
            self.cloud_backup_button.clicked.connect(self.cloud_backup)
            self.cloud_restore_button = QPushButton("云端恢复")
            self.cloud_restore_button.clicked.connect(self.cloud_restore)
            self.full_restore_button = QPushButton("完整恢复到本机")
            self.full_restore_button.clicked.connect(self.start_full_restore)
            self.queue_refresh_button = QPushButton("刷新队列")
            self.queue_refresh_button.clicked.connect(self.refresh_queue)
            self.snapshot_refresh_button = QPushButton("刷新版本")
            self.snapshot_refresh_button.clicked.connect(self.refresh_snapshots)
            self.start_config_button = QPushButton("开始配置")
            self.start_config_button.clicked.connect(self.open_settings)
            buttons = (
                self.refresh_button,
                self.repair_button,
                self.local_backup_button,
                self.cloud_backup_button,
                self.cloud_restore_button,
                self.full_restore_button,
                self.queue_refresh_button,
                self.snapshot_refresh_button,
                self.start_config_button,
            )
            for index, button in enumerate(buttons):
                action_layout.addWidget(button, index // 3, index % 3)
            layout.addWidget(action_box)
            layout.addStretch(1)
            self.overview_tab = overview
            self.tabs.addTab(overview, "概览")

        def _build_settings_tab(self) -> None:
            page = QWidget()
            layout = QVBoxLayout(page)

            cloud_box = QGroupBox("Supabase 项目")
            cloud_form = QFormLayout(cloud_box)
            self.project_url_edit = QLineEdit()
            self.project_url_edit.setPlaceholderText("https://PROJECT.supabase.co")
            self.publishable_key_edit = QLineEdit()
            self.publishable_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.publishable_key_edit.setPlaceholderText("Publishable / anon key")
            self.show_key_checkbox = QCheckBox("显示 Key")
            self.show_key_checkbox.toggled.connect(
                lambda checked: self.publishable_key_edit.setEchoMode(
                    QLineEdit.EchoMode.Normal
                    if checked
                    else QLineEdit.EchoMode.Password
                )
            )
            cloud_form.addRow("Project URL", self.project_url_edit)
            cloud_form.addRow("Publishable Key", self.publishable_key_edit)
            cloud_form.addRow("", self.show_key_checkbox)
            cloud_buttons = QWidget()
            cloud_buttons_layout = QHBoxLayout(cloud_buttons)
            self.test_connection_button = QPushButton("测试连接")
            self.test_connection_button.clicked.connect(self.test_cloud_connection)
            self.save_config_button = QPushButton("保存配置")
            self.save_config_button.clicked.connect(self.save_cloud_configuration)
            self.clear_config_button = QPushButton("清除配置")
            self.clear_config_button.clicked.connect(self.clear_cloud_configuration)
            cloud_buttons_layout.addWidget(self.test_connection_button)
            cloud_buttons_layout.addWidget(self.save_config_button)
            cloud_buttons_layout.addWidget(self.clear_config_button)
            cloud_form.addRow(cloud_buttons)
            layout.addWidget(cloud_box)

            account_box = QGroupBox("Codex Sync 账号")
            account_form = QFormLayout(account_box)
            self.email_edit = QLineEdit()
            self.email_edit.setPlaceholderText("user@example.com")
            self.password_edit = QLineEdit()
            self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.password_edit.setPlaceholderText("密码")
            self.account_state_label = QLabel("当前账号: 未读取")
            account_form.addRow("邮箱", self.email_edit)
            account_form.addRow("密码", self.password_edit)
            account_form.addRow(self.account_state_label)
            account_buttons = QWidget()
            account_buttons_layout = QHBoxLayout(account_buttons)
            self.login_button = QPushButton("登录")
            self.login_button.clicked.connect(self.sign_in)
            self.signup_button = QPushButton("注册")
            self.signup_button.clicked.connect(self.sign_up)
            self.logout_button = QPushButton("退出登录")
            self.logout_button.clicked.connect(self.sign_out)
            account_buttons_layout.addWidget(self.login_button)
            account_buttons_layout.addWidget(self.signup_button)
            account_buttons_layout.addWidget(self.logout_button)
            account_form.addRow(account_buttons)
            layout.addWidget(account_box)
            layout.addStretch(1)
            self.settings_tab = page
            self.tabs.addTab(page, "账号 / 云端设置")

        def _build_workspace_tab(self) -> None:
            page = QWidget()
            layout = QVBoxLayout(page)
            self.workspace_table = self._table(
                ("来源 Workspace 名称", "来源设备路径", "当前映射路径", "状态", "目录"),
            )
            layout.addWidget(self.workspace_table)
            buttons = QWidget()
            buttons_layout = QHBoxLayout(buttons)
            refresh_button = QPushButton("刷新 Workspace")
            refresh_button.clicked.connect(self.refresh_workspaces)
            self.bulk_map_button = QPushButton("全部映射到一个总目录")
            self.bulk_map_button.clicked.connect(self.choose_bulk_mapping_root)
            buttons_layout.addWidget(refresh_button)
            buttons_layout.addWidget(self.bulk_map_button)
            layout.addWidget(buttons)
            self.workspace_tab = page
            self.tabs.addTab(page, "Workspace")

        def _build_recovery_tab(self) -> None:
            page = QWidget()
            layout = QVBoxLayout(page)
            self.recovery_table = self._table(("阶段", "状态", "说明"))
            self.recovery_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            layout.addWidget(self.recovery_table)
            self.recovery_result = QTextEdit()
            self.recovery_result.setReadOnly(True)
            self.recovery_result.setPlaceholderText("恢复完成后显示结果")
            layout.addWidget(self.recovery_result)
            self.recovery_start_button = QPushButton("完整恢复到本机")
            self.recovery_start_button.clicked.connect(self.start_full_restore)
            layout.addWidget(self.recovery_start_button)
            self.recovery_tab = page
            self.tabs.addTab(page, "完整恢复")

        def _build_queue_tab(self) -> None:
            page = QWidget()
            layout = QVBoxLayout(page)
            self.queue_table = self._table(
                ("状态", "对象", "Hash", "尝试次数", "下次尝试", "错误"),
            )
            layout.addWidget(self.queue_table)
            self.queue_tab = page
            self.tabs.addTab(page, "上传队列")

        def _build_snapshot_tab(self) -> None:
            page = QWidget()
            layout = QVBoxLayout(page)
            self.snapshot_table = self._table(
                ("Snapshot ID", "类型", "标签", "状态", "Thread", "Session", "创建时间"),
            )
            layout.addWidget(self.snapshot_table)
            buttons = QWidget()
            buttons_layout = QHBoxLayout(buttons)
            self.snapshot_create_button = QPushButton("创建手动版本")
            self.snapshot_create_button.clicked.connect(self.create_snapshot)
            self.snapshot_restore_button = QPushButton("恢复选中版本")
            self.snapshot_restore_button.clicked.connect(self.restore_snapshot)
            refresh_button = QPushButton("刷新版本")
            refresh_button.clicked.connect(self.refresh_snapshots)
            buttons_layout.addWidget(self.snapshot_create_button)
            buttons_layout.addWidget(self.snapshot_restore_button)
            buttons_layout.addWidget(refresh_button)
            layout.addWidget(buttons)
            self.snapshot_tab = page
            self.tabs.addTab(page, "版本历史")

        @staticmethod
        def _table(headers: tuple[str, ...]) -> QTableWidget:
            table = QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.Stretch
            )
            return table

        def _start_worker(
            self,
            worker: QObject,
            finished_signal: Any,
            callback: Callable[..., None],
        ) -> None:
            thread = QThread(self)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            finished_signal.connect(callback)
            finished_signal.connect(thread.quit)
            finished_signal.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda: self._forget_worker(thread, worker))
            self._threads.append(thread)
            self._workers.append(worker)
            thread.start()

        def _run(
            self,
            arguments: list[str],
            callback: Callable[[int, object], None],
            *,
            secret_inputs: list[str] | None = None,
            environment: Mapping[str, str] | None = None,
        ) -> None:
            worker = CommandWorker(
                self._runner,
                arguments,
                secret_inputs=secret_inputs,
                environment=environment,
            )
            self._start_worker(worker, worker.finished, callback)

        def _run_sequence(
            self,
            commands: list[list[str]],
            callback: Callable[[int, object], None],
        ) -> None:
            worker = CommandSequenceWorker(self._runner, commands)
            self._start_worker(worker, worker.finished, callback)

        def _forget_worker(self, thread: QThread, worker: QObject) -> None:
            if thread in self._threads:
                self._threads.remove(thread)
            if worker in self._workers:
                self._workers.remove(worker)

        def _handle(
            self,
            action: str,
            callback: Callable[[dict[str, Any]], None],
            code: int,
            payload: object,
            *,
            dialog: bool = True,
        ) -> bool:
            data = payload if isinstance(payload, dict) else {}
            if code != 0 or not data.get("ok"):
                raw_error = str(data.get("error") or f"{action} failed")
                message = human_error(raw_error)
                self._append_log(f"{action}: {raw_error}")
                if dialog:
                    QMessageBox.critical(self, action, message)
                return False
            self._append_log(f"{action}: 完成")
            callback(data)
            return True

        def _append_log(self, message: str) -> None:
            self.log.append(message)

        def _load_configuration(self) -> None:
            try:
                config = load_supabase_config()
            except Exception:
                self._set_configured_state(False)
                return
            self.project_url_edit.setText(config.project_url)
            self.publishable_key_edit.setText(config.public_key)
            self._set_configured_state(True)

        def _set_configured_state(self, configured: bool) -> None:
            self._cloud_configured = configured
            self.cloud_config_label.setText(
                f"云端配置: {'已配置' if configured else '未配置'}"
            )
            self.start_config_button.setVisible(not configured)
            self.login_button.setEnabled(configured)
            self.signup_button.setEnabled(configured)
            if not configured:
                self._signed_in = False
                self.account_label.setText("账号: 云端未配置")
                self.account_state_label.setText("当前账号: 云端未配置")
                self.cloud_connection_label.setText("云端连接: 未配置")
                self.logout_button.setEnabled(False)

        def _set_auth_state(self, account: dict[str, Any] | None) -> None:
            email = str((account or {}).get("email") or "").strip()
            self._signed_in = bool(email)
            if email:
                self.account_label.setText(f"账号: {email}")
                self.account_state_label.setText(f"当前账号: {email}")
                self.email_edit.setText(email)
            elif self._cloud_configured:
                self.account_label.setText("账号: 未登录")
                self.account_state_label.setText("当前账号: 未登录")
            else:
                self.account_label.setText("账号: 云端未配置")
                self.account_state_label.setText("当前账号: 云端未配置")
            self.logout_button.setEnabled(self._signed_in)

        def open_settings(self) -> None:
            self.tabs.setCurrentWidget(self.settings_tab)

        def refresh_overview(self) -> None:
            worker = StartupWorker(self._runner)
            self._start_worker(worker, worker.finished, self._apply_startup)

        def _apply_startup(
            self,
            results: dict[str, tuple[int, dict[str, Any]]],
        ) -> None:
            status_code, status = results.get("status", (1, {}))
            if status_code == 0 and status.get("ok"):
                self._apply_status(status)
            else:
                self.local_label.setText("本地 Codex: 未检测")
                self._append_log(
                    f"刷新状态: {status.get('error') or '无法读取本地状态'}"
                )

            device_code, device_info = results.get("device_info", (1, {}))
            if device_code == 0 and device_info.get("ok"):
                self._apply_device_info(device_info)

            auth_code, auth = results.get("auth", (1, {}))
            if auth_code != 0 or not auth.get("ok"):
                error = str(auth.get("error") or "auth-status failed")
                if is_unconfigured_error(error):
                    self._set_configured_state(False)
                    self._append_log("云端尚未配置")
                else:
                    self._set_configured_state(True)
                    self.cloud_connection_label.setText("云端连接: 异常")
                    self._set_auth_state(None)
                    self._append_log(f"读取账号: {error}")
                self.new_device_box.setVisible(False)
                return

            self._set_configured_state(True)
            self.cloud_connection_label.setText("云端连接: 正常")
            account = auth.get("account") if auth.get("signed_in") else None
            self._set_auth_state(account if isinstance(account, dict) else None)
            if not self._signed_in:
                self.new_device_box.setVisible(False)
                return

            workspace_code, workspace_payload = results.get("workspaces", (1, {}))
            if workspace_code == 0 and workspace_payload.get("ok"):
                self._apply_workspaces(workspace_payload)
            snapshot_code, snapshot_payload = results.get("snapshots", (1, {}))
            if snapshot_code == 0 and snapshot_payload.get("ok"):
                self._apply_snapshots(snapshot_payload)
            devices_code, devices_payload = results.get("devices", (1, {}))
            if devices_code == 0 and devices_payload.get("ok"):
                self._last_devices = [
                    row
                    for row in devices_payload.get("devices", [])
                    if isinstance(row, dict)
                ]
            self._update_new_device_hint()

        def _apply_device_info(self, data: dict[str, Any]) -> None:
            self._last_device_info = data
            device = data.get("device") or {}
            self.device_label.setText(
                f"设备: {device.get('device_name') or '未知'}"
            )

        def _apply_status(self, data: dict[str, Any]) -> None:
            self._last_status = data
            self.local_label.setText(
                f"本地 Codex: {'已检测' if data.get('codex_home') else '未检测'}"
            )
            self.counts_label.setText(
                f"Thread: {data.get('total_threads', 0)}    "
                f"Session: {data.get('session_file_count', 0)}    "
                f"Index: {data.get('indexed_threads', 0)}"
            )
            self.repair_label.setText(
                f"待修复: {data.get('movable_threads', 0)}    "
                f"数据库: {data.get('movable_database_threads', 0)}    "
                f"Session: {data.get('movable_session_threads', 0)}"
            )

        def _update_new_device_hint(self) -> None:
            visible = should_suggest_new_device(
                self._last_status,
                self._snapshot_rows,
                self._last_device_info,
                self._last_devices,
            )
            self.new_device_box.setVisible(visible)

        def _validated_cloud_inputs(self) -> SupabaseConfig | None:
            url = self.project_url_edit.text().strip()
            key = self.publishable_key_edit.text().strip()
            if is_forbidden_client_key(key):
                QMessageBox.warning(
                    self,
                    "云端设置",
                    "不能在桌面客户端使用 secret、service_role 或管理员 Key。",
                )
                return None
            try:
                return SupabaseConfig(url, key)
            except Exception as exc:
                QMessageBox.warning(self, "云端设置", human_error(exc))
                return None

        def test_cloud_connection(self) -> None:
            config = self._validated_cloud_inputs()
            if config is None:
                return
            self._run(
                ["auth-status"],
                lambda code, payload: self._handle(
                    "测试连接",
                    self._connection_test_succeeded,
                    code,
                    payload,
                ),
                environment={
                    SUPABASE_URL_ENV: config.project_url,
                    SUPABASE_KEY_ENV: config.public_key,
                },
            )

        def _connection_test_succeeded(self, data: dict[str, Any]) -> None:
            self.cloud_connection_label.setText("云端连接: 正常")
            message = (
                "云端配置可用，当前账号已连接。"
                if data.get("signed_in")
                else "云端配置可用，请继续登录 Codex Sync 账号。"
            )
            QMessageBox.information(self, "测试连接", message)

        def save_cloud_configuration(self) -> None:
            config = self._validated_cloud_inputs()
            if config is None:
                return
            self._run(
                ["cloud-configure", "--url", config.project_url],
                self._configuration_saved,
                secret_inputs=[config.public_key],
            )

        def _configuration_saved(self, code: int, payload: object) -> None:
            self._handle(
                "保存配置",
                lambda _data: self._after_configuration_saved(),
                code,
                payload,
            )

        def _after_configuration_saved(self) -> None:
            self._set_configured_state(True)
            self.cloud_connection_label.setText("云端连接: 待登录")
            self.refresh_overview()

        def clear_cloud_configuration(self) -> None:
            if os.environ.get(SUPABASE_URL_ENV) or os.environ.get(SUPABASE_KEY_ENV):
                QMessageBox.warning(
                    self,
                    "清除配置",
                    "当前云端配置来自环境变量，无法在 GUI 中清除。",
                )
                return
            config_path = default_app_paths().config_path
            config_path.unlink(missing_ok=True)
            self.project_url_edit.clear()
            self.publishable_key_edit.clear()
            self._set_configured_state(False)
            self._append_log("清除配置: 完成")

        def sign_in(self) -> None:
            self._run_auth_command("auth-sign-in", "登录")

        def sign_up(self) -> None:
            self._run_auth_command("auth-sign-up", "注册")

        def _run_auth_command(self, command: str, action: str) -> None:
            email = self.email_edit.text().strip()
            password = self.password_edit.text()
            if not email or not password:
                QMessageBox.warning(self, action, "邮箱和密码不能为空。")
                return
            self._run(
                [command, "--email", email],
                lambda code, payload: self._auth_command_finished(
                    action,
                    password,
                    code,
                    payload,
                ),
                secret_inputs=[password],
            )

        def _auth_command_finished(
            self,
            action: str,
            password: str,
            code: int,
            payload: object,
        ) -> None:
            self.password_edit.clear()
            data = _redact_value(payload, (password,))

            def apply(data: dict[str, Any]) -> None:
                if action == "注册" and data.get("email_confirmation_required"):
                    QMessageBox.information(
                        self,
                        action,
                        "注册成功，请完成邮箱确认后再登录。",
                    )
                self.refresh_overview()

            self._handle(action, apply, code, data)

        def sign_out(self) -> None:
            self._run(
                ["auth-sign-out"],
                lambda code, payload: self._handle(
                    "退出登录",
                    lambda _data: self._after_sign_out(),
                    code,
                    payload,
                ),
            )

        def _after_sign_out(self) -> None:
            self._set_auth_state(None)
            self.cloud_connection_label.setText("云端连接: 待登录")
            self.new_device_box.setVisible(False)

        def repair_local(self) -> None:
            self._run(
                ["sync"],
                lambda code, payload: self._handle(
                    "本地修复",
                    lambda _data: self.refresh_overview(),
                    code,
                    payload,
                ),
            )

        def local_backup(self) -> None:
            self._run(
                ["backup"],
                lambda code, payload: self._handle(
                    "本地备份",
                    lambda _data: self.refresh_overview(),
                    code,
                    payload,
                ),
            )

        def cloud_backup(self) -> None:
            self._run(
                ["cloud-backup"],
                lambda code, payload: self._handle(
                    "云端备份",
                    lambda _data: self.refresh_snapshots(),
                    code,
                    payload,
                ),
            )

        def cloud_restore(self) -> None:
            self._run(
                ["cloud-restore"],
                lambda code, payload: self._handle(
                    "云端恢复",
                    lambda _data: self.refresh_overview(),
                    code,
                    payload,
                ),
            )

        def refresh_workspaces(self) -> None:
            self._run(
                ["workspace-list"],
                lambda code, payload: self._handle(
                    "读取 Workspace",
                    self._apply_workspaces,
                    code,
                    payload,
                ),
            )

        def _apply_workspaces(self, data: dict[str, Any]) -> None:
            self._workspace_rows = [
                row for row in data.get("workspaces", []) if isinstance(row, dict)
            ]
            self.workspace_table.setRowCount(len(self._workspace_rows))
            for row_index, row in enumerate(self._workspace_rows):
                values = (
                    row.get("name"),
                    row.get("cloud_device_path"),
                    row.get("local_path"),
                    "已映射" if row.get("mapped") else "未映射",
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value or ""))
                    if column == 0:
                        item.setData(
                            Qt.ItemDataRole.UserRole,
                            str(row.get("workspace_id") or ""),
                        )
                    self.workspace_table.setItem(row_index, column, item)
                choose_button = QPushButton("选择目录")
                choose_button.clicked.connect(
                    lambda _checked=False, index=row_index: self.choose_workspace_directory(
                        index
                    )
                )
                self.workspace_table.setCellWidget(row_index, 4, choose_button)

        def choose_workspace_directory(self, row_index: int) -> None:
            if row_index < 0 or row_index >= len(self._workspace_rows):
                return
            row = self._workspace_rows[row_index]
            initial = str(row.get("local_path") or Path.home())
            selected = QFileDialog.getExistingDirectory(
                self,
                "选择 Workspace 本地目录",
                initial,
            )
            if selected:
                self.map_workspace_row(row_index, Path(selected))

        def map_workspace_row(self, row_index: int, path: Path) -> None:
            if row_index < 0 or row_index >= len(self._workspace_rows):
                return
            workspace_id = str(
                self._workspace_rows[row_index].get("workspace_id") or ""
            )
            if not workspace_id:
                QMessageBox.warning(self, "映射 Workspace", "Workspace 数据无效。")
                return
            self._run(
                [
                    "workspace-map",
                    "--workspace-id",
                    workspace_id,
                    "--path",
                    str(path),
                ],
                lambda code, payload: self._handle(
                    "保存 Workspace 映射",
                    lambda _data: self.refresh_workspaces(),
                    code,
                    payload,
                ),
            )

        def choose_bulk_mapping_root(self) -> None:
            selected = QFileDialog.getExistingDirectory(
                self,
                "选择 Workspace 总目录",
                str(Path.home()),
            )
            if selected:
                self.map_all_workspaces_to(Path(selected))

        def map_all_workspaces_to(self, root: Path) -> None:
            mappings = build_bulk_mapping_targets(self._workspace_rows, root)
            if not mappings:
                QMessageBox.warning(self, "批量映射", "没有可映射的 Workspace。")
                return
            commands: list[list[str]] = []
            for workspace_id, target in mappings:
                target.mkdir(parents=True, exist_ok=True)
                commands.append(
                    [
                        "workspace-map",
                        "--workspace-id",
                        workspace_id,
                        "--path",
                        str(target),
                    ]
                )
            self._run_sequence(
                commands,
                lambda code, payload: self._handle(
                    "批量映射 Workspace",
                    lambda _data: self.refresh_workspaces(),
                    code,
                    payload,
                ),
            )

        def begin_guided_restore(self) -> None:
            if any(not row.get("mapped") for row in self._workspace_rows):
                self.tabs.setCurrentWidget(self.workspace_tab)
                self.refresh_workspaces()
                return
            self.start_full_restore()

        def _confirm_restore(self) -> bool:
            box = QMessageBox(self)
            box.setWindowTitle("完整恢复到本机")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(
                "将从云端恢复历史到当前电脑。\n\n"
                "程序会先创建本机安全备份，再恢复 Thread、Session 和附件。\n\n"
                "建议先暂停正在运行的 Codex 任务。\n\n"
                "是否继续？"
            )
            continue_button = box.addButton(
                "继续",
                QMessageBox.ButtonRole.AcceptRole,
            )
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            return box.clickedButton() is continue_button

        def _reset_recovery_steps(self) -> None:
            self.recovery_table.setRowCount(len(RECOVERY_STEPS))
            for row, (_key, label) in enumerate(RECOVERY_STEPS):
                self.recovery_table.setItem(row, 0, QTableWidgetItem(label))
                self.recovery_table.setItem(row, 1, QTableWidgetItem("等待中"))
                self.recovery_table.setItem(row, 2, QTableWidgetItem(""))
            self.recovery_result.clear()

        def _set_recovery_progress(
            self,
            step: str,
            status: str,
            detail: str,
        ) -> None:
            row = next(
                (
                    index
                    for index, (step_key, _label) in enumerate(RECOVERY_STEPS)
                    if step_key == step
                ),
                -1,
            )
            if row < 0:
                return
            self.recovery_table.setItem(row, 1, QTableWidgetItem(status))
            self.recovery_table.setItem(row, 2, QTableWidgetItem(detail))
            self._append_log(f"{dict(RECOVERY_STEPS).get(step, step)}: {status}")

        def start_full_restore(self) -> None:
            if not self._confirm_restore():
                return
            self.tabs.setCurrentWidget(self.recovery_tab)
            self._reset_recovery_steps()
            self.full_restore_button.setEnabled(False)
            self.recovery_start_button.setEnabled(False)
            worker = RecoveryWorker(self._runner)
            worker.progress.connect(self._set_recovery_progress)
            self._start_worker(worker, worker.finished, self._recovery_finished)

        def _recovery_finished(self, code: int, payload: object) -> None:
            self.full_restore_button.setEnabled(True)
            self.recovery_start_button.setEnabled(True)
            data = payload if isinstance(payload, dict) else {}
            if code != 0 or not data.get("ok"):
                message = human_error(data.get("error"))
                destination = data.get("destination")
                self._append_log(f"完整恢复: {data.get('error') or message}")
                if destination == "settings":
                    self.tabs.setCurrentWidget(self.settings_tab)
                elif destination == "workspace":
                    self.tabs.setCurrentWidget(self.workspace_tab)
                    self.refresh_workspaces()
                QMessageBox.warning(self, "完整恢复到本机", message)
                return
            report = build_recovery_report(data)
            self.recovery_result.setPlainText(report)
            QMessageBox.information(self, "完整恢复到本机", report)
            self.refresh_overview()

        def refresh_queue(self) -> None:
            self._run(
                ["queue-status"],
                lambda code, payload: self._handle(
                    "读取队列",
                    self._apply_queue,
                    code,
                    payload,
                ),
            )

        def _apply_queue(self, data: dict[str, Any]) -> None:
            rows = data.get("queue") or []
            self.queue_table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                values = (
                    row.get("status"),
                    row.get("object_key"),
                    row.get("content_hash"),
                    row.get("attempt_count"),
                    row.get("next_attempt_at"),
                    row.get("last_error"),
                )
                for column, value in enumerate(values):
                    self.queue_table.setItem(
                        row_index,
                        column,
                        QTableWidgetItem(str(value or "")),
                    )

        def refresh_snapshots(self) -> None:
            self._run(
                ["cloud-snapshot-list"],
                lambda code, payload: self._handle(
                    "读取版本",
                    self._apply_snapshots,
                    code,
                    payload,
                ),
            )

        def _apply_snapshots(self, data: dict[str, Any]) -> None:
            self._snapshot_rows = [
                row for row in data.get("snapshots", []) if isinstance(row, dict)
            ]
            self.snapshot_table.setRowCount(len(self._snapshot_rows))
            for row_index, row in enumerate(self._snapshot_rows):
                values = (
                    row.get("id"),
                    row.get("snapshot_type"),
                    row.get("label"),
                    row.get("status"),
                    row.get("thread_count"),
                    row.get("session_count"),
                    row.get("created_at"),
                )
                for column, value in enumerate(values):
                    self.snapshot_table.setItem(
                        row_index,
                        column,
                        QTableWidgetItem(str(value or "")),
                    )

        def create_snapshot(self) -> None:
            self._run(
                ["cloud-snapshot-create"],
                lambda code, payload: self._handle(
                    "创建版本",
                    lambda _data: self.refresh_snapshots(),
                    code,
                    payload,
                ),
            )

        def restore_snapshot(self) -> None:
            row = self.snapshot_table.currentRow()
            if row < 0 or row >= len(self._snapshot_rows):
                QMessageBox.warning(self, "恢复版本", "请先选择一个版本")
                return
            snapshot_id = str(self._snapshot_rows[row].get("id") or "")
            self._run(
                ["cloud-snapshot-restore", "--snapshot-id", snapshot_id],
                lambda code, payload: self._handle(
                    "恢复版本",
                    lambda _data: self.refresh_overview(),
                    code,
                    payload,
                ),
            )


def main() -> int:
    if not PYSIDE6_AVAILABLE:
        print(
            "PySide6 is not installed. Install the optional GUI dependency with "
            "`python -m pip install .[gui]`."
        )
        return 2
    app = QApplication(sys.argv)
    window = HistorySyncWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
