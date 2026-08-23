from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_sync.gui_flow import (
    GuiFlowError,
    build_bulk_mapping_targets,
    build_recovery_report,
    human_error,
    run_full_restore,
    safe_workspace_folder_name,
    should_suggest_new_device,
)


def successful_responses() -> dict[str, list[dict[str, object]]]:
    return {
        "status": [
            {
                "ok": True,
                "total_threads": 0,
                "session_file_count": 0,
                "indexed_threads": 0,
                "movable_threads": 0,
                "codex_home": r"C:\TestComputerB\.codex",
            },
            {
                "ok": True,
                "total_threads": 33,
                "session_file_count": 33,
                "indexed_threads": 33,
                "movable_threads": 0,
                "missing_session_index_entries": 0,
                "codex_home": r"C:\TestComputerB\.codex",
            },
        ],
        "auth-status": [
            {
                "ok": True,
                "signed_in": True,
                "account": {"email": "user@example.com"},
            }
        ],
        "workspace-list": [
            {
                "ok": True,
                "workspaces": [
                    {
                        "workspace_id": "workspace-1",
                        "name": "project",
                        "mapped": True,
                        "local_path": r"C:\Recovered\project",
                    }
                ],
            }
        ],
        "backup": [{"ok": True, "backup_path": r"C:\backup.bak"}],
        "cloud-restore": [
            {
                "ok": True,
                "summary": {
                    "cloud_threads": 33,
                    "cloud_sessions": 33,
                },
            }
        ],
        "cloud-restore-attachments": [
            {
                "ok": True,
                "summary": {
                    "cloud_attachments": 21,
                    "downloaded_objects": 21,
                },
            }
        ],
        "sync": [{"ok": True, "updated_rows": 33}],
        "probe-attachments": [
            {
                "ok": True,
                "summary": {
                    "references": 116,
                    "existing": 116,
                    "missing": 0,
                    "issues": 0,
                },
            }
        ],
    }


class ScriptedRunner:
    def __init__(self, responses: dict[str, list[dict[str, object]]]) -> None:
        self.responses = {
            command: list(items) for command, items in responses.items()
        }
        self.calls: list[str] = []
        self.arguments: list[list[str]] = []

    def __call__(self, arguments: list[str]) -> tuple[int, dict[str, object]]:
        command = arguments[0]
        self.calls.append(command)
        self.arguments.append(list(arguments))
        queue = self.responses.get(command)
        if not queue:
            return 1, {"ok": False, "error": f"Unexpected command: {command}"}
        payload = queue.pop(0)
        return (0 if payload.get("ok") else 1), payload


class GuiFlowTests(unittest.TestCase):
    def test_human_error_translates_common_cloud_failures(self) -> None:
        self.assertIn(
            "尚未配置云端服务",
            human_error("Supabase is not configured"),
        )
        self.assertIn(
            "请先登录",
            human_error("Sign in to Codex Sync before listing Workspaces"),
        )
        self.assertIn(
            "项目目录未映射",
            human_error("Workspace abc is not mapped on this device"),
        )
        self.assertIn(
            "请使用完整恢复处理冲突",
            human_error("Session content conflict: relative_path=sessions/example.jsonl"),
        )

    def test_windows_workspace_name_is_safe(self) -> None:
        self.assertEqual(
            safe_workspace_folder_name('api:project<>"/\\|?*'),
            "api_project________",
        )
        self.assertEqual(safe_workspace_folder_name("CON"), "_CON")
        self.assertTrue(safe_workspace_folder_name("", "abcdef12").startswith("Workspace-"))

    def test_bulk_mapping_targets_are_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            targets = build_bulk_mapping_targets(
                [
                    {"workspace_id": "one", "name": "project"},
                    {"workspace_id": "two", "name": "project"},
                ],
                Path(temp_dir),
            )
            self.assertEqual(targets[0][1].name, "project")
            self.assertEqual(targets[1][1].name, "project-2")

    def test_new_device_hint_requires_cloud_history_and_sparse_local_data(self) -> None:
        suggested = should_suggest_new_device(
            {"total_threads": 0, "session_file_count": 0},
            [
                {
                    "status": "complete",
                    "thread_count": 33,
                    "session_count": 33,
                }
            ],
            {"device": {"id": "device-new"}},
            [{"id": "device-new", "last_backup_at": None}],
        )
        self.assertTrue(suggested)

    def test_new_device_hint_is_hidden_after_this_device_backed_up(self) -> None:
        suggested = should_suggest_new_device(
            {"total_threads": 0, "session_file_count": 0},
            [
                {
                    "status": "complete",
                    "thread_count": 33,
                    "session_count": 33,
                }
            ],
            {"device": {"id": "device-new"}},
            [{"id": "device-new", "last_backup_at": "2026-08-21T00:00:00Z"}],
        )
        self.assertFalse(suggested)

    def test_unconfigured_cloud_stops_restore_before_workspace(self) -> None:
        runner = ScriptedRunner(
            {
                "status": [{"ok": True}],
                "auth-status": [
                    {
                        "ok": False,
                        "error": "Supabase is not configured",
                    }
                ],
            }
        )
        with self.assertRaises(GuiFlowError) as raised:
            run_full_restore(runner)
        self.assertEqual(raised.exception.destination, "settings")
        self.assertEqual(runner.calls, ["status", "auth-status"])

    def test_unsigned_account_stops_restore_before_workspace(self) -> None:
        runner = ScriptedRunner(
            {
                "status": [{"ok": True}],
                "auth-status": [{"ok": True, "signed_in": False}],
            }
        )
        with self.assertRaises(GuiFlowError) as raised:
            run_full_restore(runner)
        self.assertEqual(raised.exception.destination, "settings")
        self.assertEqual(runner.calls, ["status", "auth-status"])

    def test_unmapped_workspace_stops_restore_before_backup(self) -> None:
        runner = ScriptedRunner(
            {
                "status": [{"ok": True}],
                "auth-status": [{"ok": True, "signed_in": True}],
                "workspace-list": [
                    {
                        "ok": True,
                        "workspaces": [
                            {
                                "workspace_id": "one",
                                "name": "project",
                                "mapped": False,
                            }
                        ],
                    }
                ],
            }
        )
        with self.assertRaises(GuiFlowError) as raised:
            run_full_restore(runner)
        self.assertEqual(raised.exception.destination, "workspace")
        self.assertEqual(
            runner.calls,
            ["status", "auth-status", "workspace-list"],
        )

    def test_full_restore_uses_existing_cli_commands_in_order(self) -> None:
        runner = ScriptedRunner(successful_responses())
        result = run_full_restore(runner)
        self.assertEqual(
            runner.calls,
            [
                "status",
                "auth-status",
                "workspace-list",
                "backup",
                "cloud-restore",
                "cloud-restore-attachments",
                "sync",
                "status",
                "probe-attachments",
            ],
        )
        self.assertEqual(
            runner.arguments[4],
            ["cloud-restore", "--replace-conflicting-sessions"],
        )
        self.assertEqual(result.final_status["total_threads"], 33)

    def test_cloud_restore_failure_stops_attachment_and_sync_steps(self) -> None:
        responses = successful_responses()
        responses["cloud-restore"] = [{"ok": False, "error": "restore failed"}]
        runner = ScriptedRunner(responses)
        with self.assertRaises(GuiFlowError):
            run_full_restore(runner)
        self.assertEqual(runner.calls[-1], "cloud-restore")
        self.assertNotIn("cloud-restore-attachments", runner.calls)
        self.assertNotIn("sync", runner.calls)

    def test_attachment_restore_failure_stops_sync_step(self) -> None:
        responses = successful_responses()
        responses["cloud-restore-attachments"] = [
            {"ok": False, "error": "attachment restore failed"}
        ]
        runner = ScriptedRunner(responses)
        with self.assertRaises(GuiFlowError):
            run_full_restore(runner)
        self.assertEqual(runner.calls[-1], "cloud-restore-attachments")
        self.assertNotIn("sync", runner.calls)

    def test_recovery_progress_reports_real_stage_states(self) -> None:
        runner = ScriptedRunner(successful_responses())
        progress: list[tuple[str, str, str]] = []
        run_full_restore(
            runner,
            lambda step, status, detail: progress.append((step, status, detail)),
        )
        self.assertIn(("backup", "进行中", "创建本机安全备份"), progress)
        self.assertIn(("complete", "成功", "恢复完成"), progress)

    def test_recovery_report_uses_existing_status_and_probe_results(self) -> None:
        runner = ScriptedRunner(successful_responses())
        report = build_recovery_report(run_full_restore(runner).to_dict())
        self.assertIn("Thread 数: 33", report)
        self.assertIn("Session 数: 33", report)
        self.assertIn("Workspace 映射数量: 1", report)
        self.assertIn("附件恢复数量: 21", report)
        self.assertIn("缺失附件数: 0", report)
        self.assertIn("建议重新打开 Codex Desktop", report)


if __name__ == "__main__":
    unittest.main()
