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
