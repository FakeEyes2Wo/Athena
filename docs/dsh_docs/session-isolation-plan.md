# 每对话独立 Runtime — 实施计划（简洁版）

Status: 待实施（active）
Owner: Athena maintainers
Last verified: 2026-08-11
Scope: 修复「同一目录下不同对话不隔离」——输出串台、transcript 串写、pause 无法按对话生效。
设计依据：`docs/athena-gui-design.md` §3.2（`session_switch` = 切会话并重放 transcript，即「断点续传」，本就是单活动会话）。

## 1. 目标与成功标准

1. 同一 `project_root` 下每个会话一份独立研究状态（state / tree / artifacts / workspaces / transcript），切换不串扰。
2. 默认单会话（`"default"`）磁盘布局与 TUI/CLI/headless 行为不变。
3. `pause`/`resume`/`stop` 作用于「当前活动会话」的 run。

**成功标准**：`uv run pytest -q tests test/unit` 全绿；`cargo check` / `vitest` 不回归；手工验证「A 跑 → 切 B → 再切回 A」时 transcript 各自完整、无串台、pause 只停当前会话。

## 2. 核心思路（为什么简洁）

前端是**单面板**（`usePipeline` 一次只显示一个 `currentSessionId`），后端 `set_project_root` 已有「`aclose` 旧 runtime → 建新 runtime → transport 重订阅」的完整换绑模式（`handler.py:94-106`、`transport.py:69-75`）。

因此把「会话」当成「目录内的子命名空间」，`session_switch` 复用同一换绑模式即可：

```
切换会话 = 关闭当前 runtime（其 state/tree/transcript 已落盘）→ 用 state_root 建目标 runtime → 重订阅
```

**不做**：runtime 注册表、事件 `session_id` 打标、每 RPC 穿透 `session_id`、前端事件过滤。切换会话即「断点续传」，旧会话 run 停止、状态持久化、切回时恢复——与设计 §3.2 一致。

## 3. 任务（3 个，全在 Python；前端零到微调）

### T1 — `ResearchRuntime` 增加 `state_root`（状态命名空间化）

- **改动** `src/athena/research/runtime.py:72-91`：`__init__` 增
  `state_root: str | Path | None = None` 与 `session_id: str = "default"`。
  - `self._athena = Path(state_root).resolve() if state_root else (self._root / ".athena")`。
  - `_state_path`/`_tree_path`/`_sessions_dir`/`_store`/`_scripts.workdir`/`_git.repo`（`94-127`）已由 `_athena` 派生，无需逐个改。
  - `workspaces`（`126`）：`state_root` 为 `None` 时保持 `self._root / "workspaces"`；否则 `state_root / "workspaces"`。
  - 把 `session_id` 传给 `RuntimeEvents`（见 T2）。
- **不变量**：`state_root=None` 时与现状逐字节一致（默认会话 + 三个非 GUI 入口零回归）。
- **验收**：`uv run pytest test/unit/research/ -q`。

### T2 — `RuntimeEvents` 去掉可变会话指针（代码更少）

- **改动** `src/athena/research/runtime_events.py`：
  - `__init__` 增 `session_id: str = "default"`，`self._session_id` 构造期绑定、不再可变。
  - 删除 `set_session()`（`278-281`）与 `sessions_list()`（`283-292`）——会话清单归网关（T3）。
  - `_log_path`（`52-55`）、`_append_log`（`263-270`）、`replay_output_events`（`305-320`，去掉 `session_id` 参数）只读写本 runtime 自己的 transcript。
- **改动** `src/athena/research/runtime.py:527-536`：删除 `sessions_list`/`set_session`；`replay_output_events()` 不带参数；保留 `persist_user_message`。
- **验收**：`grep -rn "set_session\|sessions_list" src/athena/research` 无残留；`uv run pytest test/unit/research/ -q`。

### T3 — 网关 `session_switch` 复用换绑模式 + 前端微调

- **改动** `src/gui_gateway/handler.py`：
  - `GuiRequestHandler` 增 `session_state_root(session_id) -> Path`：`"default"` → `project_root/.athena`；否则 `project_root/.athena/sessions/{session_id}`。**校验 session_id 为安全 basename**（复用 `require_safe_path_basename` 同款，拒绝 `..`/绝对路径/非法字符）。
  - `session_switch(session_id)`：`await self._runtime.aclose()`（持久化并停旧 run）→ `self._runtime = self._make_runtime(project_root, state_root=..., session_id=...)` → `self._service = GuiService(self._runtime)` → 返回 `{"records": self._runtime.replay_output_events()}`。**与 `set_project_root`（`94-106`）同一形状**，`transport.py` 的换绑重订阅（`69-75`）无需改动即可生效。
  - `sessions_list()`：`["default"] + sorted(.athena/sessions/* 子目录名)`。
  - `set_project_root` 记录 `self._project_root`，供 `session_state_root` 使用。
- **改动** `src/gui_gateway/__main__.py`：`_make_runtime` 增 `state_root`/`session_id` 透传参数。
- **前端**（`athena-gui/src/hooks/usePipeline.ts`）：**基本无需改**——`newSession` 仍 `sessionSwitch("s-" + Date.now())`（后端对未知 id 建空 runtime），`switchSession` 仍 `sessionSwitch(id)` + `restoreRecords`。仅确认 `newSession` 的 `sessionSwitch(id).catch()` 在空 transcript 下正确清空视图。
- **验收**：`uv run pytest tests/test_gui_protocol_contract.py -q`（`SUPPORTED_METHODS` 不变）；手工双会话验证。

## 4. 依赖与顺序

```
T1（state_root）→ T2（去可变会话指针）→ T3（网关换绑 + 前端确认）
```

- T1 是地基；T2 依赖 T1（transcript 落各自 state_root）；T3 依赖 T1/T2。
- 三个任务各自独立提交。

## 5. 验证命令

```bash
uv run pytest -q tests test/unit
grep -rn "set_session" src/ || echo "OK"
cd athena-gui/src-tauri && cargo check
cd athena-gui && pnpm vitest run
```

## 6. 验收标准（逐任务）

- [ ] T1：`state_root` 使状态/树/artifacts/repo/workspaces 落会话命名空间；`None` 时零回归。
- [ ] T2：`RuntimeEvents` 无 `set_session`/`sessions_list`；每 runtime 只写自身 transcript。
- [ ] T3：`session_switch` 换绑 runtime；`sessions_list` 列 `default` + 子命名空间；session_id 路径穿越被拒；切换不串台、pause 只停当前会话。

## 7. 决策点与风险

- **磁盘布局**：默认会话 `state_root = .athena`（完全向后兼容）；新会话 `.athena/sessions/{id}`。session_id 作为路径分量，**必须**安全 basename 校验。
- **切换即关闭旧 run**：切会话会 `aclose` 旧 runtime（停其后台 run，状态已持久化，切回时「断点续传」）。这是单面板 + 设计 §3.2 的语义；如需「多会话并发后台跑」，才需 registry 方案（本次不做，保持简洁）。
- **workspaces / git repo**：默认会话保持 `project_root/workspaces` + `.athena/repo`；新会话用 `state_root/workspaces` + `state_root/repo`（各自隔离）。
- **seq 命名空间**：每 runtime 的 `EventProjector` seq 独立从 0 起；前端按 `currentSessionId` 重建视图、消息 id 用 `seq`，跨会话切换不冲突（联调时验证）。
- **回退纪律**：非 GUI 入口（TUI/CLI/headless）直接 `ResearchRuntime(project_root=...)`，`state_root=None` 保持现状，零回归。

## 8. 规范引用

- 设计契约：`docs/athena-gui-design.md` §3.2（`sessions_list`/`session_switch` 断点续传语义）。
- 状态模型：`src/athena/research/runtime.py`、`runtime_events.py`。
- 安全：session_id 路径校验复用 `require_safe_path_basename` 同款（`src/athena/app_server/thread_manager.py`）。
- 代码规范：`docs/代码规范.md`（简洁：复用既有换绑模式，不引入新抽象）。
