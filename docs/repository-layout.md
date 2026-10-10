# 项目目录导航

核对日期：2026-10-10。根目录保留安装、配置、许可证和构建入口；
历史设计与零散笔记统一进入 `docs/archive/`。

| 路径 | 职责与入口 |
|---|---|
| [src/athena/](../src/athena/) | Python 主应用；`cli.py` 为 CLI 入口，`research/` 为研究流程，`execution/` 为执行后端 |
| [src/athena_tui/](../src/athena_tui/) | TUI；由 `pyproject.toml` 的 `Athena-tui` 启动 |
| [src/gui_gateway/](../src/gui_gateway/) | GUI 的 Python WebSocket gateway |
| [athena-gui/](../athena-gui/) | Web GUI、Tauri 桌面壳与前端测试；根 `package.json` 转发开发/构建命令 |
| [athena-rust/](../athena-rust/) | Rust workspace；保留其 Cargo 布局与测试 |
| [tests/](../tests/) | Python 单元、集成、架构测试及跨语言共享 JSON fixtures；pytest 默认入口 |
| [scripts/](../scripts/) | 开发、打包、诊断与检查工具 |
| [docker/](../docker/) | 容器配置 |
| `submission/reproduction/`（本地） | 离线复现材料；整个 `submission/` 被 Git 忽略，不随克隆分发 |
| [docs/](README.md) | 用户/开发文档、专题、阅读记录与历史归档 |
| [codex_docs/CURRENT.md](../codex_docs/CURRENT.md) | AGENTS.md 指定的工作索引；所链接的计划和报告主要放在 `docs/codex_docs/` |
| [docs/archive/](archive/README.md) | 原 `code_spec/` 与根目录工作笔记，均为历史材料 |
| [docs/reviews/](reviews/) | 有源码证据的审查发现；与实施完成报告分开 |

`pyproject.toml`/`uv.lock`、`package.json`/`package-lock.json`、配置模板、
`AGENTS.md` 和许可证留在根目录，保持安装和工具发现路径稳定。
语言组件内的测试目录服务于各自工具链，不合并到根 Python 测试目录。

## 本地文件与生成目录

`build/`、`dist/`、`.venv/`、`node_modules/`、`.athena/`、`workspaces/`
及 Python 缓存由现有忽略规则管理。根测试目录统一为 `tests/`；旧 `test/`
确认只有 357 个 `.pyc` 缓存，已清除，未丢弃测试源码。
按用户要求，独立的 `athena_ts/` 已从本地和 Git 跟踪中全部删除；
构建脚本只接受 Python 后端，Web GUI 的 TypeScript 前端仍然保留。

本次开始前已存在的 `release-artifacts/`、`ATHENA_ERRORS.md`、`init_prompt.md`
和根 `codex_docs/` 中两份未跟踪报告原位保留，未纳入整理提交。
既有 `submission/` 文件保留，`reproduction/` 整体移入其中并退出 Git 跟踪。
本地运行复现材料时，可先 `cd submission`，再按材料中的 `reproduction/...`
相对路径执行命令；Python 环境路径按实际位置调整。

整理修正了 Tauri 测试的旧 `test/fixtures` 引用，使 Rust、前端和 Python
使用同一份 `tests/fixtures/clarification/`。运行验证见
[整理完成报告](codex_docs/2026-10-10-repository-organization-completion.md)。
