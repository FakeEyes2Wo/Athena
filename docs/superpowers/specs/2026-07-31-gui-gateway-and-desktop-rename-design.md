# GUI Gateway 与桌面应用重命名设计

## 目标

消除项目中不准确的 `IDE` 命名，将 Python WebSocket 适配层命名为其真实职责 `gui_gateway`，并将桌面应用目录及包元数据统一为 `athena-gui`。

本次只调整命名、目录和引用，不改变 RPC 协议、运行时所有权或界面行为。

## 当前职责

`src/athena/ide/` 不负责 GUI 渲染，也不拥有研究状态。它承担三项边界职责：

1. 启动本地 WebSocket 服务并输出动态端口；
2. 校验请求与封装响应；
3. 将桌面客户端请求和事件在 WebSocket 与 `ResearchRuntime` 之间转发。

因此该组件应命名为 gateway，而不是 IDE、backend 或单一 handler。

## 目标结构

```text
src/
  athena/
    ...
  gui_gateway/
    __init__.py
    __main__.py
    handler.py
    transport.py

athena-gui/
  package.json
  src/
  src-tauri/
```

Python 入口由 `python -m athena.ide` 改为 `python -m gui_gateway`。不保留 `athena.ide` 兼容包，避免两个入口长期并存。

## 命名变更

| 当前名称 | 目标名称 |
| --- | --- |
| `src/athena/ide/` | `src/gui_gateway/` |
| `athena.ide` | `gui_gateway` |
| `IDEHandler` | `GuiRequestHandler` |
| `athena-ide/` | `athena-gui/` |
| npm/Cargo 包名 `athena-ide` | `athena-gui` |
| Rust 库名 `athena_ide_lib` | `athena_gui_lib` |
| Tauri identifier `com.athena.ide` | `com.athena.gui` |
| `scripts/dev-ide.*` | `scripts/dev-gui.*` |

`WebSocketTransport` 保持原名，因为它已经准确描述职责。

## 引用迁移

实现时同步更新以下有效引用：

- Python 包内导入与模块入口；
- Python 单元、传输层和端到端测试，并将 `test_ide_*` 更名为 `test_gui_gateway_*`；
- Tauri Python bridge 的启动模块和仓库根目录推导；
- npm lockfile、Cargo lockfile、Tauri 配置和 Rust 库调用；
- Windows 与 Unix 开发脚本；
- 当前架构文档及 README 中的源码路径。

历史设计与实施记录保留当时名称，不机械改写 `docs/superpowers/` 中既有文档。被忽略的本地工作树也不在迁移范围内。

## 打包与运行

`pyproject.toml` 的 Hatch wheel 配置需同时包含 `src/athena` 与 `src/gui_gateway`，确保顶层 gateway 包进入构建产物。桌面应用仍从仓库根目录启动 `uv run python -m gui_gateway`，RPC 数据流保持：

```text
athena-gui -> WebSocket -> gui_gateway -> athena.research.ResearchRuntime
```

迁移后的 Tauri bridge 位于 `athena-gui/src-tauri/`，目录深度不变，仓库根目录仍按两级回溯计算：`src-tauri -> athena-gui -> repository root`。相关注释与错误信息必须使用新名称。

## 兼容性

这是一次显式破坏性重命名：

- `athena.ide` 导入不再可用；
- `python -m athena.ide` 不再可用；
- `scripts/dev-ide.*` 不再保留；
- Tauri bundle identifier 改变后，操作系统会把新构建视为新的应用身份。

仓库内所有调用方将在同一变更中迁移，因此不提供弃用周期。

## 验证

迁移完成后至少执行：

1. `uv run pytest tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py tests/test_gui_gateway_transport_import.py`
2. 在 `athena-gui/` 执行 `npm test` 与 `npm run build`
3. 在 `athena-gui/src-tauri/` 执行 `cargo check`
4. 执行 `uv run python -c "import gui_gateway; from gui_gateway.__main__ import start_server"`
5. 搜索有效源码、测试、脚本与当前文档，确认不再引用 `athena.ide`、`athena-ide`、`IDEHandler` 或 `dev-ide`
6. 执行 `git diff --check` 与 pre-commit 检查

## 完成标准

- Python gateway 仅存在于 `src/gui_gateway/`；
- 桌面应用仅存在于 `athena-gui/`；
- 包名、类名、启动入口、脚本与当前文档一致；
- Python、前端和 Rust 验证通过；
- 当前工作区中与本次迁移相关的旧路径和旧名称全部消失。
