from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any


def execute_cli(arguments: list[str]) -> tuple[int, dict[str, Any]]:
    """Run the JSON CLI in-process so the same code works from source and EXE."""
    from codex_sync import cli

    previous_argv = sys.argv
    output = io.StringIO()
    try:
        sys.argv = ["codex-history-sync", "--json", *arguments]
        with redirect_stdout(output):
            exit_code = cli.main()
    finally:
        sys.argv = previous_argv
    text = output.getvalue().strip()
    if not text:
        return exit_code, {"ok": False, "error": "CLI returned no output"}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return exit_code, {"ok": False, "error": text}
    if not isinstance(payload, dict):
        return exit_code, {"ok": False, "error": "CLI returned an invalid JSON object"}
    return exit_code, payload


def main() -> int:
    try:
        from PySide6.QtCore import QObject, QThread, Signal
        from PySide6.QtWidgets import (
            QApplication,
            QAbstractItemView,
            QFormLayout,
            QGridLayout,
            QGroupBox,
            QHeaderView,
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
            QLabel,
        )
    except ImportError:
        print(
            "PySide6 is not installed. Install the optional GUI dependency with "
            "`python -m pip install .[gui]`."
        )
        return 2

    class CommandWorker(QObject):
        finished = Signal(int, object)

        def __init__(self, arguments: list[str]) -> None:
            super().__init__()
            self.arguments = arguments

        def run(self) -> None:
            code, payload = execute_cli(self.arguments)
            self.finished.emit(code, payload)

    class HistorySyncWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("Codex History Sync")
            self.resize(1120, 760)
            self._threads: list[QThread] = []
            self._workers: list[CommandWorker] = []
            self._snapshot_rows: list[dict[str, Any]] = []
            self._build_ui(
                QGridLayout,
                QGroupBox,
                QAbstractItemView,
                QHeaderView,
                QLineEdit,
                QFormLayout,
                QTableWidget,
                QTableWidgetItem,
                QTabWidget,
                QTextEdit,
                QVBoxLayout,
                QWidget,
                QLabel,
                QPushButton,
            )
            self.refresh_overview()

        def _build_ui(
            self,
            QGridLayout: Any,
            QGroupBox: Any,
            QAbstractItemView: Any,
            QHeaderView: Any,
            QLineEdit: Any,
            QFormLayout: Any,
            QTableWidget: Any,
            QTableWidgetItem: Any,
            QTabWidget: Any,
            QTextEdit: Any,
            QVBoxLayout: Any,
            QWidget: Any,
            QLabel: Any,
            QPushButton: Any,
        ) -> None:
            root = QWidget()
            root_layout = QVBoxLayout(root)
            self.tabs = QTabWidget()
            root_layout.addWidget(self.tabs)
            self.log = QTextEdit()
            self.log.setReadOnly(True)
            self.log.setPlaceholderText("任务日志")
            root_layout.addWidget(self.log)
            self.setCentralWidget(root)

            overview = QWidget()
            overview_grid = QGridLayout(overview)
            self.account_label = QLabel("账号: 未读取")
            self.local_label = QLabel("本地状态: 未读取")
            self.counts_label = QLabel("Thread / Session: 未读取")
            self.repair_label = QLabel("待修复: 未读取")
            for row, widget in enumerate(
                (
                    self.account_label,
                    self.local_label,
                    self.counts_label,
                    self.repair_label,
                )
            ):
                overview_grid.addWidget(widget, row, 0, 1, 3)

            action_box = QGroupBox("本地与云端操作")
            action_layout = QGridLayout(action_box)
            actions = (
                ("刷新状态", self.refresh_overview),
                ("本地修复", self.repair_local),
                ("本地备份", self.local_backup),
                ("云端备份", self.cloud_backup),
                ("云端恢复", self.cloud_restore),
                ("刷新队列", self.refresh_queue),
                ("刷新版本", self.refresh_snapshots),
            )
            for index, (label, callback) in enumerate(actions):
                button = QPushButton(label)
                button.clicked.connect(callback)
                action_layout.addWidget(button, index // 3, index % 3)
            overview_grid.addWidget(action_box, 5, 0, 1, 3)
            self.tabs.addTab(overview, "概览")

            workspace_page = QWidget()
            workspace_layout = QVBoxLayout(workspace_page)
            self.workspace_table = self._table(
                QTableWidget,
                QAbstractItemView,
                QHeaderView,
                ("Workspace ID", "名称", "当前目录", "来源设备目录", "已映射"),
            )
            workspace_layout.addWidget(self.workspace_table)
            map_box = QGroupBox("映射当前设备目录")
            map_form = QFormLayout(map_box)
            self.workspace_id_edit = QLineEdit()
            self.workspace_path_edit = QLineEdit()
            map_form.addRow("Workspace ID", self.workspace_id_edit)
            map_form.addRow("本地目录", self.workspace_path_edit)
            map_button = QPushButton("保存映射")
            map_button.clicked.connect(self.map_workspace)
            map_form.addRow(map_button)
            workspace_layout.addWidget(map_box)
            workspace_refresh = QPushButton("刷新 Workspace")
            workspace_refresh.clicked.connect(self.refresh_workspaces)
            workspace_layout.addWidget(workspace_refresh)
            self.tabs.addTab(workspace_page, "Workspace")

            queue_page = QWidget()
            queue_layout = QVBoxLayout(queue_page)
            self.queue_table = self._table(
                QTableWidget,
                QAbstractItemView,
                QHeaderView,
                ("状态", "对象", "Hash", "尝试次数", "下次尝试", "错误"),
            )
            queue_layout.addWidget(self.queue_table)
            self.tabs.addTab(queue_page, "上传队列")

            snapshot_page = QWidget()
            snapshot_layout = QVBoxLayout(snapshot_page)
            self.snapshot_table = self._table(
                QTableWidget,
                QAbstractItemView,
                QHeaderView,
                ("Snapshot ID", "类型", "标签", "状态", "Thread", "Session", "创建时间"),
            )
            self.snapshot_table.setSelectionBehavior(QAbstractItemView.SelectRows)
            snapshot_layout.addWidget(self.snapshot_table)
            snapshot_buttons = QWidget()
            snapshot_buttons_layout = QGridLayout(snapshot_buttons)
            snapshot_create = QPushButton("创建手动版本")
            snapshot_create.clicked.connect(self.create_snapshot)
            snapshot_restore = QPushButton("恢复选中版本")
            snapshot_restore.clicked.connect(self.restore_snapshot)
            snapshot_refresh = QPushButton("刷新版本")
            snapshot_refresh.clicked.connect(self.refresh_snapshots)
            snapshot_buttons_layout.addWidget(snapshot_create, 0, 0)
            snapshot_buttons_layout.addWidget(snapshot_restore, 0, 1)
            snapshot_buttons_layout.addWidget(snapshot_refresh, 0, 2)
            snapshot_layout.addWidget(snapshot_buttons)
            self.tabs.addTab(snapshot_page, "版本历史")

        @staticmethod
        def _table(
            QTableWidget: Any,
            QAbstractItemView: Any,
            QHeaderView: Any,
            headers: tuple[str, ...],
        ) -> Any:
            table = QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            table.setSelectionBehavior(QAbstractItemView.SelectRows)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            return table

        def _run(self, arguments: list[str], callback: Any) -> None:
            thread = QThread(self)
            worker = CommandWorker(arguments)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.finished.connect(callback)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda: self._forget_worker(thread, worker))
            self._threads.append(thread)
            self._workers.append(worker)
            thread.start()

        def _forget_worker(self, thread: QThread, worker: CommandWorker) -> None:
            if thread in self._threads:
                self._threads.remove(thread)
            if worker in self._workers:
                self._workers.remove(worker)

        def _handle(self, action: str, callback: Any, code: int, payload: object) -> None:
            data = payload if isinstance(payload, dict) else {}
            if code != 0 or not data.get("ok"):
                error = str(data.get("error") or f"{action} failed")
                self._append_log(f"{action}: {error}")
                QMessageBox.critical(self, action, error)
                return
            self._append_log(f"{action}: 完成")
            callback(data)

        def _append_log(self, message: str) -> None:
            self.log.append(message)

        def refresh_overview(self) -> None:
            self._run(
                ["status"],
                lambda code, payload: self._handle(
                    "刷新状态",
                    self._apply_status,
                    code,
                    payload,
                ),
            )
            self.refresh_auth_status()

        def refresh_auth_status(self) -> None:
            self._run(
                ["auth-status"],
                lambda code, payload: self._handle(
                    "读取账号",
                    self._apply_auth,
                    code,
                    payload,
                ),
            )

        def _apply_auth(self, data: dict[str, Any]) -> None:
            account = data.get("account") or {}
            self.account_label.setText(
                f"账号: {account.get('email') or '未登录'}"
            )

        def _apply_status(self, data: dict[str, Any]) -> None:
            self.local_label.setText(f"本地状态: {data.get('codex_home') or '未知'}")
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

        def repair_local(self) -> None:
            self._run(
                ["sync"],
                lambda code, payload: self._handle(
                    "本地修复",
                    lambda _: self.refresh_overview(),
                    code,
                    payload,
                ),
            )

        def local_backup(self) -> None:
            self._run(
                ["backup"],
                lambda code, payload: self._handle(
                    "本地备份",
                    lambda _: self.refresh_overview(),
                    code,
                    payload,
                ),
            )

        def cloud_backup(self) -> None:
            self._run(
                ["cloud-backup"],
                lambda code, payload: self._handle(
                    "云端备份",
                    lambda _: self.refresh_snapshots(),
                    code,
                    payload,
                ),
            )

        def cloud_restore(self) -> None:
            self._run(
                ["cloud-restore"],
                lambda code, payload: self._handle(
                    "云端恢复",
                    lambda _: self.refresh_overview(),
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
            rows = data.get("workspaces") or []
            self.workspace_table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                values = (
                    row.get("workspace_id"),
                    row.get("name"),
                    row.get("local_path"),
                    row.get("cloud_device_path"),
                    "是" if row.get("mapped") else "否",
                )
                for column, value in enumerate(values):
                    self.workspace_table.setItem(
                        row_index,
                        column,
                        QTableWidgetItem(str(value or "")),
                    )

        def map_workspace(self) -> None:
            workspace_id = self.workspace_id_edit.text().strip()
            path = self.workspace_path_edit.text().strip()
            if not workspace_id or not path:
                QMessageBox.warning(self, "映射 Workspace", "Workspace ID 和目录不能为空")
                return
            self._run(
                ["workspace-map", "--workspace-id", workspace_id, "--path", path],
                lambda code, payload: self._handle(
                    "保存 Workspace 映射",
                    lambda _: self.refresh_workspaces(),
                    code,
                    payload,
                ),
            )

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
                    lambda _: self.refresh_snapshots(),
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
                    lambda _: self.refresh_overview(),
                    code,
                    payload,
                ),
            )

    app = QApplication(sys.argv)
    window = HistorySyncWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
