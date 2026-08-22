from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


CommandRunner = Callable[[list[str]], tuple[int, dict[str, Any]]]
ProgressCallback = Callable[[str, str, str], None]

RECOVERY_STEPS = (
    ("prepare", "准备恢复"),
    ("auth", "检查登录"),
    ("workspace", "检查 Workspace"),
    ("backup", "创建本机安全备份"),
    ("history", "恢复 Thread / Session"),
    ("attachments", "恢复图片 / 附件"),
    ("repair", "执行本地修复"),
    ("verify", "检查恢复结果"),
    ("complete", "完成"),
)

_INVALID_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class GuiFlowError(RuntimeError):
    def __init__(self, message: str, *, destination: str | None = None) -> None:
        super().__init__(message)
        self.destination = destination


@dataclass(frozen=True)
class RecoveryResult:
    initial_status: dict[str, Any]
    final_status: dict[str, Any]
    workspaces: list[dict[str, Any]]
    backup: dict[str, Any]
    history_restore: dict[str, Any]
    attachment_restore: dict[str, Any]
    local_repair: dict[str, Any]
    attachment_probe: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_status": self.initial_status,
            "final_status": self.final_status,
            "workspaces": self.workspaces,
            "backup": self.backup,
            "history_restore": self.history_restore,
            "attachment_restore": self.attachment_restore,
            "local_repair": self.local_repair,
            "attachment_probe": self.attachment_probe,
        }


def human_error(error: object) -> str:
    text = str(error or "").strip()
    lowered = text.casefold()
    if "supabase is not configured" in lowered:
        return "尚未配置云端服务，请先完成云端设置。"
    if (
        "not signed in" in lowered
        or "sign in to codex sync" in lowered
        or "no saved codex sync login session" in lowered
    ):
        return "请先登录 Codex Sync 账号。"
    if "workspace" in lowered and (
        "not mapped" in lowered
        or "unmapped" in lowered
        or "directory does not exist" in lowered
    ):
        return "还有项目目录未映射，请先选择新电脑上的保存目录。"
    if "invalid login credentials" in lowered:
        return "登录失败，请检查邮箱和密码。"
    return text or "操作失败，请查看任务日志。"


def is_unconfigured_error(error: object) -> bool:
    return "supabase is not configured" in str(error or "").casefold()


def safe_workspace_folder_name(name: object, workspace_id: object = "") -> str:
    value = _INVALID_WINDOWS_NAME.sub("_", str(name or "").strip())
    value = re.sub(r"\s+", " ", value).rstrip(" .")
    if not value:
        suffix = str(workspace_id or "").strip()[:8] or "workspace"
        value = f"Workspace-{suffix}"
    stem = value.split(".", 1)[0].upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        value = f"_{value}"
    return value[:80].rstrip(" .") or "Workspace"


def build_bulk_mapping_targets(
    rows: Iterable[dict[str, Any]],
    root: Path,
) -> list[tuple[str, Path]]:
    target_root = root.expanduser().resolve(strict=False)
    used: set[str] = set()
    mappings: list[tuple[str, Path]] = []
    for row in rows:
        workspace_id = str(row.get("workspace_id") or "").strip()
        if not workspace_id:
            continue
        base_name = safe_workspace_folder_name(row.get("name"), workspace_id)
        candidate = base_name
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base_name}-{suffix}"
            suffix += 1
        used.add(candidate.casefold())
        mappings.append((workspace_id, target_root / candidate))
    return mappings


def should_suggest_new_device(
    local_status: dict[str, Any],
    snapshots: list[dict[str, Any]],
    device_info: dict[str, Any],
    devices: list[dict[str, Any]],
) -> bool:
    device = device_info.get("device") if isinstance(device_info, dict) else None
    if not isinstance(device, dict):
        return False
    device_id = str(device.get("id") or "")
    current_cloud_device = next(
        (item for item in devices if str(item.get("id") or "") == device_id),
        None,
    )
    if not isinstance(current_cloud_device, dict):
        return False
    if current_cloud_device.get("last_backup_at"):
        return False

    complete_snapshots = [
        item for item in snapshots if str(item.get("status") or "") == "complete"
    ]
    cloud_threads = max(
        (int(item.get("thread_count") or 0) for item in complete_snapshots),
        default=0,
    )
    cloud_sessions = max(
        (int(item.get("session_count") or 0) for item in complete_snapshots),
        default=0,
    )
    if cloud_threads <= 0 and cloud_sessions <= 0:
        return False

    local_threads = int(local_status.get("total_threads") or 0)
    local_sessions = int(local_status.get("session_file_count") or 0)
    threads_are_sparse = cloud_threads > 0 and (
        local_threads == 0 or local_threads * 2 < cloud_threads
    )
    sessions_are_sparse = cloud_sessions > 0 and (
        local_sessions == 0 or local_sessions * 2 < cloud_sessions
    )
    return threads_are_sparse or sessions_are_sparse


def run_full_restore(
    runner: CommandRunner,
    progress: ProgressCallback | None = None,
) -> RecoveryResult:
    update = progress or (lambda _step, _status, _detail: None)

    def command(step: str, arguments: list[str]) -> dict[str, Any]:
        code, payload = runner(arguments)
        data = payload if isinstance(payload, dict) else {}
        if code != 0 or not data.get("ok"):
            error = str(data.get("error") or f"{arguments[0]} failed")
            update(step, "失败", human_error(error))
            raise GuiFlowError(human_error(error))
        return data

    update("prepare", "进行中", "读取当前本机状态")
    initial_status = command("prepare", ["status"])
    update("prepare", "成功", "本机状态已读取")

    update("auth", "进行中", "检查 Codex Sync 登录状态")
    code, auth_payload = runner(["auth-status"])
    auth = auth_payload if isinstance(auth_payload, dict) else {}
    if code != 0 or not auth.get("ok"):
        raw_error = str(auth.get("error") or "auth-status failed")
        message = human_error(raw_error)
        update("auth", "失败", message)
        destination = "settings" if is_unconfigured_error(raw_error) else "settings"
        raise GuiFlowError(message, destination=destination)
    if not auth.get("signed_in"):
        message = "请先登录 Codex Sync 账号。"
        update("auth", "失败", message)
        raise GuiFlowError(message, destination="settings")
    update("auth", "成功", "账号已登录")

    update("workspace", "进行中", "检查项目目录映射")
    workspace_payload = command("workspace", ["workspace-list"])
    workspaces = [
        row
        for row in workspace_payload.get("workspaces", [])
        if isinstance(row, dict)
    ]
    unmapped = [row for row in workspaces if not row.get("mapped")]
    if unmapped:
        message = "还有项目目录未映射，请先选择新电脑上的保存目录。"
        update("workspace", "失败", f"{message} 未映射: {len(unmapped)}")
        raise GuiFlowError(message, destination="workspace")
    update("workspace", "成功", f"已映射 {len(workspaces)} 个 Workspace")

    update("backup", "进行中", "创建本机安全备份")
    backup = command("backup", ["backup"])
    update("backup", "成功", "本机安全备份已创建")

    update("history", "进行中", "恢复 Thread 和 Session")
    history_restore = command("history", ["cloud-restore"])
    update("history", "成功", "Thread 和 Session 恢复完成")

    update("attachments", "进行中", "恢复图片和附件")
    attachment_restore = command(
        "attachments",
        ["cloud-restore-attachments"],
    )
    update("attachments", "成功", "图片和附件恢复完成")

    update("repair", "进行中", "修复本地 Provider、Model 和索引")
    local_repair = command("repair", ["sync"])
    update("repair", "成功", "本地修复完成")

    update("verify", "进行中", "检查本机状态和附件引用")
    final_status = command("verify", ["status"])
    attachment_probe = command("verify", ["probe-attachments"])
    update("verify", "成功", "恢复结果检查完成")
    update("complete", "成功", "恢复完成")

    return RecoveryResult(
        initial_status=initial_status,
        final_status=final_status,
        workspaces=workspaces,
        backup=backup,
        history_restore=history_restore,
        attachment_restore=attachment_restore,
        local_repair=local_repair,
        attachment_probe=attachment_probe,
    )


def build_recovery_report(result: dict[str, Any]) -> str:
    status = result.get("final_status") or {}
    workspaces = result.get("workspaces") or []
    attachment_restore = result.get("attachment_restore") or {}
    attachment_summary = attachment_restore.get("summary") or {}
    probe = result.get("attachment_probe") or {}
    probe_summary = probe.get("summary") or {}
    lines = [
        "恢复完成",
        "",
        f"Thread 数: {status.get('total_threads', 0)}",
        f"Session 数: {status.get('session_file_count', 0)}",
        f"Index 数: {status.get('indexed_threads', 0)}",
        f"Workspace 映射数量: {sum(bool(row.get('mapped')) for row in workspaces if isinstance(row, dict))}",
        f"附件恢复数量: {attachment_summary.get('cloud_attachments', 0)}",
        f"缺失附件数: {probe_summary.get('missing', 0)}",
        f"待修复数: {status.get('movable_threads', 0)}",
        f"缺失 Index 数: {status.get('missing_session_index_entries', 0)}",
        f"本地 Codex Home: {status.get('codex_home') or '未知'}",
    ]
    issues = int(probe_summary.get("issues") or 0)
    if issues:
        lines.append(f"附件扫描问题: {issues}")
    lines.extend(("", "建议重新打开 Codex Desktop 查看历史记录。"))
    return "\n".join(lines)
