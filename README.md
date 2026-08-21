# Codex History Sync Tool

一个用于恢复 Codex Desktop 本地历史对话显示的小工具。

当你切换 API、provider、模型或登录方式之后，Codex Desktop 有时会出现“本地历史明明还在，但侧边栏看不到”的情况。这个工具会检查本机的本地历史数据库、会话文件和侧边栏索引，并把旧线程重新挂到当前正在使用的 `model_provider` / `model` 下面。

## 这个工具能做什么

- 查看当前本机 Codex 历史线程属于哪些 provider
- 查看当前本机 Codex 历史线程属于哪些 model
- 一键把旧 provider / model 下的线程、会话元数据和侧边栏索引同步到当前设置
- 自动识别根目录或 `sqlite` 子目录中最近仍有活动的 Codex 状态数据库
- Codex Desktop 正在运行时也可以同步；如果本地数据库正在写入，工具会等待空闲后继续
- 在同步前自动备份数据库、侧边栏索引和会话元数据
- 从备份恢复数据库
- 提供一个可直接点击的 Windows 图形界面

## 适用场景

- 你切换了不同 API
- 你切换了不同 provider
- 你切换了不同模型
- 你切换了登录方式
- 你确认本地历史文件还在，但 Codex Desktop 左侧历史列表变空了

## V1 不适用的场景

- 云端账号之间的聊天记录互相同步
- 本地历史文件已经被删除
- 不同电脑之间迁移聊天记录

V2 正在 `v2-development` 分阶段增加云端账号、设备、上传、恢复、附件和版本管理。当前已完成本地修复兼容层、附件探测、Supabase Schema/RLS 设计、Auth/Devices、Thread / Session 手动增量上传，以及纯文本历史下载恢复。

## 运行环境

- Windows
- PowerShell 5.1 或更高版本
- 已安装 Python 3.10 或更高版本，并可通过 `py -3` 调用
- 本机存在 Codex Desktop 本地数据目录，通常是 `%USERPROFILE%\\.codex`

## 快速使用

### 图形界面

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\launch_ui.ps1
```

### 创建桌面快捷方式

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\launch_ui.ps1 -InstallShortcutOnly
```

### 查看当前状态

```powershell
py -3 .\sync_backend.py --json status
```

### 执行同步

```powershell
py -3 .\sync_backend.py --json sync
```

新版官方配置可能不再写入 `model_provider`。此时工具会安全地使用官方默认值 `openai`，不会从旧历史记录猜测当前账号。如果你使用第三方 Provider，或者自动识别结果不符合实际，可以手动指定目标：

```powershell
py -3 .\sync_backend.py --json sync --provider openai
py -3 .\sync_backend.py --json sync --provider your-provider --model your-model
```

`--model` 留空时，如果 `config.toml` 里也没有 `model`，工具只修正 Provider，不会批量改写已有线程的模型。

### 手动创建备份

```powershell
py -3 .\sync_backend.py --json backup
```

### 探测图片和附件

```powershell
py -3 .\sync_backend.py --json probe-attachments
```

该命令只读扫描 Session JSONL、归档 Session、Codex 附件清单、内嵌图片和本地图片路径，输出 Thread / Session / Message 关联、文件类型、大小、SHA256、引用位置和文件存在状态。它不会输出内嵌图片正文，也不会把普通项目路径和命令输出路径当作附件。

### 配置 Codex Sync 云端账号

```powershell
py -3 .\sync_backend.py --json cloud-configure --url https://PROJECT.supabase.co
```

命令会隐藏输入客户端可公开使用的 Supabase publishable/anon key。桌面客户端会拒绝 secret/service-role key。

### 注册、登录和查看账号

```powershell
py -3 .\sync_backend.py --json auth-sign-up --email user@example.com
py -3 .\sync_backend.py --json auth-sign-in --email user@example.com
py -3 .\sync_backend.py --json auth-status
py -3 .\sync_backend.py --json auth-sign-out
```

密码只通过隐藏提示读取。登录 Session 使用 Windows DPAPI 加密后保存在 `%LOCALAPPDATA%\CodexHistorySync\sync_state.sqlite`。

### 本机设备

```powershell
py -3 .\sync_backend.py --json device-info
py -3 .\sync_backend.py --json device-register
py -3 .\sync_backend.py --json device-list
```

第一次运行会生成稳定的本机 `device_id`。云端注册和列表需要先完成 Supabase V2 兼容迁移与登录。

### 手动上传 Thread 和 Session

```powershell
py -3 .\sync_backend.py --json cloud-backup
```

该命令只读扫描 Codex 状态数据库，通过白名单上传 Thread 元数据，并把 Session JSONL 按 SHA256 内容寻址保存到私有 Storage。未变化的 Session 不会重复上传；上传后会重新读取云端 Session 清单并逐项校验 Hash，验证成功后才更新设备的 `last_backup_at`。

当前连接的 Supabase 原型 Schema 与仓库中的 V2 migrations 不兼容。必须先在干净项目部署 `migrations/001` 至 `009`，或完成经过审查的兼容迁移，才能实际执行该命令。详见 `docs/MANUAL-CLOUD-BACKUP.md`。

### 从云端恢复纯文本历史

```powershell
py -3 .\sync_backend.py --json cloud-restore
py -3 .\sync_backend.py --json cloud-restore --thread-id THREAD_UUID
py -3 .\sync_backend.py --json cloud-restore --target-cwd E:\Work\Recovered
```

恢复命令只下载本机缺失的 Session，先在临时目录校验大小、SHA256 和 `session_meta`，再创建本地安全备份并写入。恢复失败会还原数据库和索引，并删除本轮新建的 Session。目标机 Provider / Model 会通过 Local Repair Engine 修复。

目标 `.codex` 必须先由 Codex Desktop 初始化。当前阶段只恢复纯文本 Thread / Session；图片、附件和完整 Workspace Mapping 在后续阶段实现。详见 `docs/PURE-TEXT-CLOUD-RESTORE.md`。

### 从最新备份恢复

```powershell
py -3 .\sync_backend.py --json restore
```

### 运行测试

```powershell
py -3 -m unittest discover -s tests -v
```

## 备份说明

- 每次同步前都会自动创建一份备份
- 每次恢复前也会先创建一份安全备份
- 备份默认保存在 `%USERPROFILE%\\.codex\\history_sync_backups`
- 新版备份会同时保存 `session_index.jsonl` 和会话文件首行元数据，恢复时会一起还原

## 状态数据库选择

Codex Desktop 不同版本可能把状态数据库放在以下任一位置：

- `%USERPROFILE%\.codex\state_5.sqlite`
- `%USERPROFILE%\.codex\sqlite\state_5.sqlite`

如果两个文件同时存在，工具会比较数据库本体和对应 `-wal` 文件的最近活动时间，选择实际仍在写入的那个；时间相同时优先根目录版本。工具不会仅因为 `sqlite` 子目录存在就使用其中可能已经过期的副本。

## 使用建议

- Codex Desktop 开着也可以同步；如果它正在生成回复或保存历史，工具可能会等待几秒
- 恢复备份会覆盖当前状态，最稳妥的做法仍然是在恢复前暂停正在运行的 Codex 任务
- 如果同步完成后历史列表没有立刻刷新，重开一次 Codex Desktop 即可
- 新版 Codex 可能还会按当前项目目录显示历史。如果同步后仍然看不到旧对话，先确认是否打开了旧对话原来的项目目录；本工具默认不会批量改写线程的 `cwd` 项目归属。

## 项目文件

- `sync_backend.py`：保持 V1 命令和 PowerShell UI 兼容的入口
- `codex_sync/cli.py`：V2 命令行入口
- `codex_sync/local/repair_engine.py`：本地历史检查、修复、备份和恢复引擎
- `codex_sync/attachments/probe.py`：只读图片和附件结构探测
- `docs/ATTACHMENT-PROBE.md`：真实格式调查结果和 Probe 边界
- `migrations/`：Supabase PostgreSQL、RLS 和 Storage 策略
- `docs/SUPABASE-SCHEMA.md`：云端 Schema、所有权和上线验证说明
- `docs/AUTH-AND-DEVICES.md`：账号、DPAPI Session 和设备系统
- `docs/MANUAL-CLOUD-BACKUP.md`：Thread / Session 手动上传、增量判断和验证规则
- `docs/PURE-TEXT-CLOUD-RESTORE.md`：纯文本云端下载、目标 Schema 适配和失败回滚
- `docs/SUPABASE-COMPATIBILITY-AUDIT.md`：现有 Supabase 原型 Schema 的只读兼容审计
- `launch_ui.ps1`：Windows 图形界面
- `CHANGELOG.md`：正式版本变更记录

## 免责声明

这个工具直接操作本机 Codex 的本地状态数据库。虽然已经做了自动备份，但仍建议你在使用前先理解它的作用，并自行确认本地数据目录状态。
