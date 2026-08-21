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

### Phase 6

- 增加 `cloud-backup`，以只读方式扫描本机 Thread 和对应 Session JSONL。
- Thread 仅上传白名单元数据，Session 路径必须位于 `sessions/` 或 `archived_sessions/`。
- Session 使用稳定文件读取、SHA256 内容寻址和云端 Hash 比较，只上传变化对象。
- 兼容 Codex 数据库中使用 `\\?\` 前缀的 Windows 扩展长度 Session 路径。
- 相同 SHA256 的 Session Storage 对象自动复用，避免重复上传。
- 上传后重新读取云端 Session 清单并逐项验证 Hash，验证成功后才记录设备备份时间。
- 增加本机增量行为、路径边界、PostgREST 冲突键和 Storage 二进制请求契约测试。

### Phase 7

- 增加 `cloud-restore`，支持恢复全部纯文本历史或指定 Codex Thread。
- 仅下载目标机缺失的 Session，并在临时目录验证大小、SHA256 和 `session_meta`。
- 增加动态 Codex Thread Schema Adapter，兼容现代与简化旧版 `threads` 表。
- 恢复时使用目标机 Provider、Model 和本地 cwd，不写入旧电脑的绝对 Session 路径。
- 写入前创建安全备份，失败时恢复数据库和索引并删除本轮新建 Session。
- 恢复完成后验证 SQLite、Thread、Session ID 和活动 Session 索引。
- 增加 TestComputerA → Cloud → TestComputerB、指定 Thread、损坏对象和失败回滚测试。

### Phase 8

- 增加 `cloud-upload-attachments`，上传 Probe 确认存在的图片和附件。
- 使用 `users/USER_ID/attachments/HASH_PREFIX/SHA256.ext` 私有 Storage 路径。
- `attachments` 按用户和 SHA256 去重，`attachment_references` 保留全部消息和清单引用。
- 同一图片的内嵌 `data:` 内容与本地缓存只上传一个对象。
- 支持已知图片、文档、压缩包扩展名，未知或不安全扩展名回退为 `.bin`。
- 缺失文件、远程 URL 和待删除清单记录不上传；缺少云端 Thread/Session 时提前失败。
- 上传后验证云端 Hash 实体和引用位置，成功后才更新设备备份时间。
- 增加 PNG、TXT、PDF、重复 Hash、二次增量和 Supabase Storage 请求契约测试。

### Phase 9

- 增加 `cloud-restore-attachments`，下载并验证云端图片和附件。
- 使用 `.codex/restored_attachments/HASH_PREFIX/SHA256.ext` 重建目标机路径。
- 按结构解析 Session JSONL，只替换 Probe 记录过的旧绝对路径和文件 URI。
- 保留内嵌 `data:` 图片，并将匹配的本地图片引用指向恢复文件。
- 支持重复恢复幂等，已存在且 Hash 正确的附件不会重复下载。
- Session 写入失败时恢复原始字节并删除本轮新附件，回滚动作逐项执行。
- 增加图片、TXT、PDF、不同电脑路径、引用修复和故障回滚测试。

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
