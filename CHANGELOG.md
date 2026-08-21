# Changelog

本项目从 `v1.0.0` 开始记录正式版本变更。

## [Unreleased]

### Phase 1

- 完成 Fork、remote、分支和 V1 稳定基线审计。
- 在 `v1-stable` tag 固定 V1 基线，保留原有 11 个测试。
- 记录真实 Codex `state_5.sqlite`、WAL、Session JSONL、归档目录和附件引用结构。
- 确认 V2 采用白名单提取、SHA256 内容寻址和 Adapter 兼容层，不上传整个 `.codex`。

### Phase 2

- 建立 `codex_sync` Python 包和 `Local Repair Engine` 模块边界。
- 将 V1 命令行解析从本地修复实现中分离。
- 保留 `sync_backend.py` 作为原 PowerShell UI、CLI 和测试的兼容入口。
- 增加兼容层自动化测试，保证 V1 导出和命令名称不变。

### Phase 3

- 增加只读 `probe-attachments` 命令和 Attachment Probe。
- 支持 Session 内嵌 `data:` 图片、本地图片路径、粘贴文本清单和归档 Session。
- 使用 SHA256 关联同一图片的内嵌内容和本地缓存文件。
- 输出 Thread、Session、Message、MIME、大小、路径、Hash、引用位置和存在状态。
- 增加匿名化现代 Session fixture，并覆盖缺失文件、归档扫描和路径白名单测试。

### Phase 4

- 增加 profiles、devices、workspaces、threads、sessions、attachments、sync events 和 snapshots migrations。
- 使用复合所有权外键阻止跨用户实体关联。
- 附件按 `(user_id, sha256)` 去重，并单独保存多条消息引用。
- 为全部公开表启用 RLS，撤销 `anon` 权限并显式授权 `authenticated`。
- 增加私有 Storage bucket 和用户路径隔离的上传、下载、覆盖、删除策略。
- 增加 migration 安全契约测试和 Supabase 上线验证清单。

### Phase 5

- 增加 Supabase 公共项目配置，并拒绝 secret/service-role key。
- 实现独立 Codex Sync 注册、登录、登出、自动刷新、登录恢复和当前账号查询。
- 使用 Windows DPAPI 加密 Auth Session，并保存在独立 `sync_state.sqlite`。
- 增加稳定本机 Device ID、设备信息、云端注册和设备列表接口。
- 密码仅通过隐藏交互输入，Token 不进入公开输出，远端错误回显会自动脱敏。
- 只读审计现有 `codex-sync` Supabase 原型，确认需要兼容 migration，未修改主项目。

## [1.0.0] - 2026-08-11

首个正式稳定版本。

### 新增

- 支持新版 Codex 的 `sqlite/state_5.sqlite` 状态数据库位置。
- 支持通过图形界面或命令行手动指定目标 Provider 和 Model。
- 支持同步数据库、会话文件和侧边栏索引，并在写入前自动备份。
- 支持数据库占用重试、备份恢复和 7 位小数时间戳兼容。

### 安全与兼容性

- 当根目录和 `sqlite` 子目录的数据库同时存在时，根据数据库及其 WAL 的最近活动时间选择实际使用中的数据库。
- 配置缺少 `model_provider` 时安全回退到官方默认值 `openai`，不再从旧历史数据猜测当前 Provider。
- 配置与命令行都未指定 Model 时保留线程原有模型，不做批量改写。
