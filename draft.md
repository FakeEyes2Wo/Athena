# src + athena_ts 简化记录

## 目标
- 保持功能与泛化性
- 删除无关代码
- 减少过度类型检查（Python 用 pyflakes；TS 用 tsc --noUnusedLocals/--noUnusedParameters）
- 减少 class 数量与 class attrs
- 降低维护者心智负担

## 基线
- src 下 Python 文件：165 个，共 30,718 行（不含 __pycache__）
- athena_ts 源文件：79 个，共 8,626 行（不含 tests/node_modules/dist）
- 验证方式：`python -m compileall -q src` + `python -m pyflakes src` + pytest；TS `npm run build` / `build:dsh` / `build:autoresearch`
- Python 测试基线（PYTHONPATH=src，忽略 2 个已知 collection 错误）：
  1278 passed, 84 failed, 2 skipped, 27 errors（多为环境性失败：git/编码/API key）

## 轮次记录

### Round 1（删除整文件死代码 + pyflakes 修复）
- 搜索：AST 扫描 src 下从未被 import 的模块；pyflakes 全量扫描。
- 修改：
  - 删除 `src/athena/retrieval/types.py`（29 行，2 个死 dataclass）
  - 删除 `src/athena/research/idea_generation/candidate_generation.py`（187 行，1 个死 class）
  - 删除 `src/athena/research/idea_generation/state.py`（34 行，1 个死 dataclass）
  - `prompts.py` 删除 GENERATION_STRATEGY_* 策略 prompt 常量块（79 行，仅被 candidate_generation 引用）
  - `execution/runtime.py` 将嵌套 `async def persist` 改为 `self._store.put_text` 直接绑定（消除 pyflakes 重定义告警）
- 验证：compileall=0，pyflakes=0。

### Round 2（删减 idea_generation 死函数与死 schema）
- 搜索：AST 扫描 src 顶层 def 是否被外部/自身引用。
- 修改：
  - `revision.py`：563 行 → 116 行。只保留 `build_revision_prompt` + `revise_candidate`；删除无人调用的
    `is_revisable` / `select_debate_opponent` / `is_no_op_revision` / `novelty_is_stale` /
    `stale_perspectives` / `build_rereview_prompt` / `blocked_item_cleared` / `run_debate` /
    `refresh_stale_evidence` 及死常量 `MAX_DEBATE_ROUNDS` / `RISK_*`。
  - `evidence_retrieval.py`：删除 `build_gap_miner_agent` / `build_novelty_agent` / `mine_research_gaps`
    三个死函数，并剪掉对应 import（create_agent / ToolRegistry / TYPE_CHECKING）。
  - `prompts.py`：删除死常量 `GAP_MINER_*` 与 `NOVELTY_SYSTEM_PROMPT`。
  - `idea_schemas.py`：删除 `ResearchProblemInput` / `GapCandidateDraft` / `GapMiningResponse` / `GapCandidate`
    四个无引用的 Pydantic 模型。
- 验证：compileall=0，pyflakes=0；`test_corpus_handoff.py` 8 passed；idea_generation 测试与基线同失败集合。

### Round 3（再次死代码扫描 + 删死 schema）
- 搜索：重新运行 AST dead-def 扫描。
- 修改：
  - `idea_schemas.py`：删除死模型 `HypothesisDraft` / `VerbalizedSamplingResponse` /
    `RevisionRound` / `PipelineCandidateResult`（4 个 class，约 100 行）。
- 验证：compileall=0，pyflakes=0；pytest 结果与基线一致：1278 passed, 84 failed, 2 skipped, 27 errors。

## 最终结果（前 3 轮）
- src 下 Python 文件：162 个（-3），共 29,852 行（-866 行）。
- 删除 3 个死模块、约 12 个死 class、约 10 个死函数/常量块。
- 行为保持证据：compileall 通过、pyflakes 0 告警、pytest 通过/失败数与基线完全一致。

---

# 追加 3 轮：src + athena_ts

### Round 4（TS noUnused 清理 + 去 attrs）
- 搜索：`tsc -p packages/* --noUnusedLocals --noUnusedParameters`。
- 修改（athena_ts）：
  - `athena-research/src/evaluation.ts`、`supervisor/plans.ts`、`supervisor/prepare.ts`：删除未使用 import。
  - `athena-research/src/runtime.ts`：删除未使用 `commit` 参数（改 `_commit`）。
  - `athena-research/src/supervisor/events.ts`：删除未使用私有字段 `store`，构造参数改 `_store`。
  - `athena-research/src/supervisor/supervisor.ts`：删除未使用字段/选项 `recovery`，`decideSettlement` 去掉未使用参数 `reportRef`，`settlePlan` 去掉未使用参数 `result`，并同步更新两处调用点。
  - `athena-dsh/src/index.ts`、`athena-research/src/runtime.ts`：删除向 `FixedFlowSupervisor` 传 `recovery` 的接线与 import。
- 验证：全部 5 个包 `tsc --noUnusedLocals --noUnusedParameters` = 0；`npm run build` / `build:dsh` / `build:autoresearch` = 0。

### Round 5（Python 死函数/死 class 再清理）
- 搜索：AST dead-def 扫描 + grep 全仓引用。
- 修改（src）：
  - `idea_generation/gate.py`：删除死函数 `gate_hypotheses` 与仅其使用的 `_GATE_PROMPT_TEMPLATE`、`FalsifiabilityJudgment` import。
  - `idea_generation/review_board.py`：删除死函数 `build_domain_consistency_agent`、`review_board`，并剪掉 `create_agent`/`ToolRegistry`/`TYPE_CHECKING` import。
  - `research/data_models.py`：删除无引用的 `ColumnSummary`、`DataProfile` 两个 class，并更新模块 docstring。
- 验证：compileall=0，pyflakes=0；idea_generation + 契约测试与基线同失败集合（6 failed, 41 passed）。

### Round 6（TS 微清理 + 最终验证）
- 搜索：TS 未使用导出扫描。
- 修改（athena_ts）：
  - `athena-research/src/supervisor/prepare.ts`：删除未使用导出常量 `PREPARE_AGENT_ID` / `EVALUATOR_AGENT_ID` / `EVALUATOR_PLAN_ID`。
- 验证：全部 TS 包 noUnused = 0，build 全通过；Python pytest 与基线一致（1278 passed, 84 failed, 2 skipped, 27 errors）。

## 追加 3 轮后最终结果
- src 下 Python 文件：162 个，共 29,743 行。
- athena_ts 源文件：79 个，共 8,609 行。
- 追加轮次合计净减：src -109 行；athena_ts -17 行（另有 attrs/参数/接线删减，未按行计）。
- 行为保持证据：Python compileall=0、pyflakes=0、pytest 与基线一致；TS 全包 noUnused=0、build 全通过。

---

# 追加：athena_ts 目录/git/state 对齐（已确认后实施）

## 对齐内容
- `runtime.ts`：
  - 新增 `stateRoot` / `ideatorCount` / `hypothesesPerIdeator` 选项。
  - 记录目录统一：`.athena/state.json`、`.athena/research_tree.json`、`.athena/artifacts`、
    `.athena/runs`、`.athena/repo`；代码目录 `root/workspaces`（配置 `stateRoot` 时变为 `.athena/workspaces`）。
  - 新增 `.athena/logs/sessions`、`.athena/logs/agents`，并在 start/message/aclose 时写入
    `runtime.jsonl` 轻量会话事件。
  - `start()` 与 `runPreparePhase` 的 `git.init` 改为初始提交 `.gitignore`（内容 `.venv/\n`），对齐 Python。
  - 新增 `save_tree()` / `load_tree()`。
  - 新增 `applySettings()` 白名单（concurrency / search_limit / ideator_count /
    hypotheses_per_ideator / manual_mode / direction / tolerance），持久字段写回 `state.json`。
  - `settings()` 增补 ideator_count / hypotheses_per_ideator / manual_mode / task_understanding / model_connection。
  - 新增 `state.eda_dir` 越界校验：跨目录拷贝来的 state 会置空 eda_dir。
- `state.ts`：
  - `ResearchState` 增补 `ideator_count`（默认 3，1..8）、`hypotheses_per_ideator`（默认 2，1..5）、
    `task_understanding`（默认 null），与 Python `research/supervisor/state.py` 对齐。

## 验证
- TS 全包 `tsc --noUnusedLocals --noUnusedParameters` = 0。
- `npm run build` / `build:dsh` / `build:autoresearch` 全部通过。

---

# 追加：athena-dsh 插件 UI 润色

## 改动（athena_ts/packages/athena-dsh/src/index.ts）
- 两个既有工具补充 DSH presentation UI：
  - `research_status`：`presentCall`（generic/search 卡片）+ `presentResult`（generic 卡片，
    展示格式化的状态行）+ `output.presentationMeta` 透传结构化状态。
  - `research_run`：`presentCall`（generic/execute 卡片，rawInput 展示任务文本）+
    `presentResult`（generic 卡片，展示启动后状态）。
- 新增只读工具 `research_tree`：
  - 返回研究树快照（version / sota_id / 假设数 / 实验数 / 待选假设 / 实验列表）。
  - 同样带 `presentCall`（generic/read 卡片）与 `presentResult`（generic 卡片）。
- `researchStatus()` 增补 `ideator_count` / `hypotheses_per_ideator` / `task_understanding`。
- 测试同步：`test/index.test.ts` 期望注册顺序更新为
  `["research_status", "research_tree", "research_run"]`。

## 验证
- 全部 TS 包 noUnused = 0；`npm run build` / `build:dsh` / `build:autoresearch` = 0。

---

# 追加：hypothesis graph 可视化（athena_ts + DSH Web UI）

## 背景
- 尝试从 CDN/npm 拉取开源图渲染库（vis-network）失败：当前沙箱网络与 npm 缓存均被拒
  （EPERM/TLS 凭据错误），因此改为自研一个零依赖 SVG 图渲染器，布局参考 d3-dag 的
  Sugiyama 分层思想（layered DAG + barycenter 交叉减少）。

## 改动
- `athena-core`：
  - 新增 `src/models/hypothesis-graph.ts`：`buildHypothesisGraph(tree)`，移植 Python
    `src/athena/gui/graph.py` 的 nodes/edges 结构（lineage 父→子、supersedes 子→被取代祖先）。
  - `src/index.ts` 导出 `buildHypothesisGraph` 与 `HypothesisGraph*` 类型。
- `athena-autoresearch` DSH Web UI：
  - `scripts/dsh-ui-server.mjs`：
    - 新增 `/api/hypothesis_graph`：优先返回 `ResearchTree` 的图；树为空时回退到
      `HypothesisPool` 构建图（节点=池条目，lineage 来自 `hypothesis.parent_id`，
      supersedes 来自 `hypothesis.supersedes`）。
    - `/api/run` 现在会加载 `.athena/research_tree.json`（存在时）并注入 `AutoResearchRuntime`。
    - 静态路由新增 `/hypothesis-graph.js`。
  - `dsh-ui/index.html`：新增「假设图」卡片与样式。
  - `dsh-ui/app.js`：启动时挂载图可视化。
  - `dsh-ui/hypothesis-graph.js`（新增）：零依赖 SVG 渲染器，支持分层布局、状态着色、
    SOTA 星标、谱系/取代两种边样式、拖拽平移、滚轮缩放、双击复位、3s 自动刷新。

## 验证
- 全部 TS 包 noUnused = 0；`npm run build` / `build:dsh` / `build:autoresearch` = 0。
- `node --check` 通过 server 与 UI 脚本。
- `buildHypothesisGraph` 用最小 ResearchTree 做了 smoke test（2 节点 0 边）。

## 后续润色：DSH UI 白天模式
- `dsh-ui/index.html` 整体改为浅色主题（#f5f7fa 背景、白色卡片、蓝色强调、柔和阴影）。
- 状态徽章按 RUNNING/WAITING/FAILED/COMPLETED 等使用绿/琥珀/红/紫浅色语义。
- 假设图卡片加图例（谱系实线、取代虚线、SOTA 星标），SVG 渲染器同步改为浅色调色板。
- `app.js` 增加连接状态胶囊（已连接/未连接）与控制台刷新时间戳。
- 服务进程已重新常驻（PID 810164，http://127.0.0.1:8787）。

## 后续润色：对齐 athena-gui 设计系统
- `dsh-ui/index.html` 复刻 `athena-gui/src/styles.css` 的 light token：
  bg/surface/border/accent/text/semantic/shadow/radius/spacing/transition 全部对齐。
- 布局改为 AppShell 结构：52px 顶栏（品牌 mark + 状态点）、48px 图标功能轨（概览/假设图/控制台
  滚动导航）、232px 上下文侧栏（数据目录 + 运行/刷新）、主工作区。
- 按钮使用 `.btn / .btn--primary / .btn--ghost`，徽章使用 `badge--success/warning/danger/neutral/info`
  与 Athena GUI 一致；图例改为悬浮在图容器右上角。
- `app.js` 状态映射对齐 Athena：RUNNING→info、WAITING→warning、COMPLETED→success、
  FAILED/STOPPED→danger；顶栏状态点同步 data-status。
- `hypothesis-graph.js` 节点/边颜色改为 Athena GUI 的 hypothesis status token
  （PROPOSED/SUPPORTED/REFUTED/REJECTED 完全同值）。

---

# 追加：参照 Codex web/run 完善 websearch 工具

参考 `C:\Users\80163\Desktop\挑战杯_2026\codex\codex-rs\ext\web-search` 的实现，
把可迁移到 DuckDuckGo 的能力落到 `src/athena/retrieval/web_search.py`：

## 改动
- 新增 `WebSession`：跨 `web_search`/`web_fetch` 共享会话，维护 `turn0search*-*` /
  `turn0fetch*` 引用，等价 Codex web.run 的 ref_id。
- `WebSearchTool`：
  - 新增 Codex 风格 `search_query` 批量查询（最多 4 条），每条支持 `q` / `domains` / `recency`。
  - `domains` → DDG `site:` 组合查询；`recency`（天）→ DDG `df=d/w/m/y`。
  - 新增 `response_length`（short=3 / medium=8 / long=15），兼容旧 `query` / `max_results`。
  - 每个结果带 `ref_id` + `query`，多查询结果按 URL 去重。
- `WebFetchTool`：
  - 新增 `ref_id`：直接打开 `web_search` 结果或已抓取页面，不重复解析。
  - 新增 `pattern`：在页面文本中大小写无关查找并返回上下文摘录（等价 Codex `find`）。
  - 抓取到的页面回写会话缓存，后续 find 不再发请求。
- `agent_turn_runner.py`：General Agent 注册时创建共享 `WebSession`，把两个工具串成一条链路。
- 测试：`test/unit/retrieval/test_web_search.py` 从 3 个扩到 10 个，覆盖批量过滤/去重、
  ref_id 打开、find、会话解析。

## 验证
- `compileall` = 0；`pyflakes`（改动文件）= 0。
- `python -m pytest -q test/unit/retrieval/test_web_search.py` → 10 passed。

## 追加：最小化 + 继续完善
- `web_search.py` 437 行 → 321 行（-116），行为保持不变：
  - 抽出 `_WebTool` 公共构造，去掉两个工具重复的 `__init__`。
  - `WebSession` 方法压缩为 `search_refs / fetch_ref / remember_* / page / url`。
  - 搜索流程合并为 `_queries` + `search` / `search_many`，抓取流程去掉重复分支。
  - `find_in_page` 返回 `{"matches", "total"}`：matches 是 capped 摘录，total 是真实命中数。
- 继续完善：
  - 新增 Codex 规则：`search_query` 超过 3 条且 `response_length=short` 时直接报错，
    要求 medium/long。
  - 测试从 10 个扩到 11 个，覆盖该规则与 total/capped 语义。
- 验证：`compileall` = 0，`pyflakes` = 0，`test_web_search.py` 11 passed。

## 追加：DSH GUI 回到底部按钮
- `dsh-ui/index.html`：新增 `.fab` 悬浮胶囊按钮（右下角、浅色 token、箭头图标 + “回到底部”），
  位于 shell 内、固定在视口右下，`[hidden]` 时隐藏。
- `dsh-ui/app.js`：监听 `.main` 滚动，距底部 < 48px 时自动隐藏按钮；点击平滑
  `scrollTo({ top: scrollHeight })`；`refresh()` 后同步按钮显隐，避免内容变化后状态过期。
- 验证：`node --check app.js` 通过；重启常驻服务（新 PID 167500），
  `http://127.0.0.1:8787/` 返回页面包含 `to-bottom` 按钮。

## 追加：修复 “unknown experiment id” 错误
- 现象：SEARCH Plan 提交后报 `research failed: 'unknown experiment id: exp_hyp_...'`。
- 根因：`startPlan()` 只保存 `state.json`，没有保存 `research_tree.json`；进程重启/状态恢复后
  `state.plans` 存在但 `research_tree.json` 缺少对应的 `exp_${hypothesisId}`。
  `Recovery.reconcile()` 又把“无 experiment 的 Plan”当作未终结 Plan 保留，最终
  `settlePlan()` 调用 `completeExperiment` 时找不到实验记录而崩溃。
- 修改：
  - `supervisor.ts` `startPlan()`：新增 experiment 后立即 `tree.save(treePath)`；
    并在“已有 Plan 但缺 experiment”时抛出明确错误，不再拖到 settle 才报未知 ID。
  - `recovery.ts`：SEARCH Plan 如果没有对应 experiment 记录，视为孤儿 Plan 直接丢弃，
    避免恢复后继续运行到崩溃。
  - 更新 `recovery.test.ts` 对应两条用例。
- 现场修复：
  - `C:\Users\80163\Desktop\挑战杯_2026\new_kaggle_test\.athena` 中
    `state.json` 有 `hyp_088d74e0c1e4` Plan，`research_tree.json` 无对应 experiment。
  - 用一次性脚本从 `context_ref` + `best_ref` 重建 `exp_hyp_088d74e0c1e4`：
    metric 7800.4667 > baseline 7784.6667 → WIN，标记 SUPPORTED 并设为新 SOTA；
    删除孤儿 Plan，state 恢复 RUNNING。
  - 验证：`ResearchTree.load` / `ResearchState.load` 均通过；当前 `new_kaggle_test`
    可继续运行。
- 验证：`tsc -p packages/athena-research` 通过；Recovery 行为用 Node 冒烟验证
  （缺 experiment 丢弃、有 RUNNING experiment 保留）。

## 追加：Prompt 鼓励对长输出做 grep 式定向搜索
- 背景：Agent 跑 `shell_command` 遇到 OpenSpiel 等超长错误/可用列表时，不应整段阅读。
- 修改：
  - `src/athena/execution/runtime.py`：`shell_command` 工具 description 增加
    “长输出先管道搜索”的指引，示例 `grep` / `findstr` / `Select-String`。
  - `athena_ts/packages/athena-research/src/shell.ts`：TS 版 `shell_command`
    工具 description 同步增加同一指引。
  - 多个 agent prompt 同步补充同一规则：
    `general_agent.md`、`plan_agent.md`、`prepare_agent.md`、`evaluator_agent.md`、
    `code_agent.md`、`data_agent.md`。
- 验证：`python -m py_compile src/athena/execution/runtime.py` 通过，
  `python -m pyflakes src/athena/execution/runtime.py` 通过；
  `tsc -p packages/athena-research` 通过。

## 追加：清理仓库临时文件
- 删除根目录临时脚本 `.inspect_loop.py`。
- 递归清理 `__pycache__`、`.pytest_cache`、`.mypy_cache`、`.ruff_cache`、
  `.hypothesis`、`.tox` 等缓存目录（共 4940 个）。
- 清理 `*.tmp` / `*.bak` / `*.orig` / `*.rej` / `*.pyc` / `*.pyo` / `~` 结尾等
  临时文件（5 个）。
- 剩余 1 个无法删除：`.worktrees/supervisor-streaming-output/.pytest_cache`
  （OS 拒绝访问，可能由外部权限/占用导致），不影响主仓库。

## 追加：Release 构建
- 执行 `node scripts/release.cjs --backend python`：
  - Python 后端 PyInstaller 打包 `gui_gateway.exe`
  - 前端 `tsc && vite build`
  - Tauri release + MSI/NSIS 安装器
- 产物：
  - `athena-gui/src-tauri/resources/gui_gateway.exe`（131,457,521 B）
  - `athena-gui/src-tauri/target/release/athena-gui.exe`（15,522,816 B）
  - `athena-gui/src-tauri/target/release/bundle/msi/Athena_0.1.0_x64_en-US.msi`（138,711,040 B）
  - `athena-gui/src-tauri/target/release/bundle/nsis/Athena_0.1.0_x64-setup.exe`（136,219,704 B）
- 构建退出码 0，全部完成。

## 追加：Python 版 “unknown experiment id” 修复（与 TS 版对齐并增强恢复）
- 现象：Python 后端 SEARCH Plan 恢复后报
  `research failed: 'unknown experiment id: exp_hyp_...'`。
- 根因：`Supervisor.start_plan` 只写 `state.json`，不写 `research_tree.json`；
  崩溃/重启后 `state.plans` 仍在，但 tree 缺 `exp_{hypothesis_id}`。
  `Recovery.reconcile` 又把无 experiment 的 SEARCH Plan 当未终结保留，最终
  `settle_plan` 查不到实验记录。
- 修改：
  - `supervisor.py` `start_plan()`：新增 experiment 后先落 `state.json`、
    再立即落 `research_tree.json`；已有 Plan 但缺 experiment 时抛明确错误。
  - `supervisor.py` `recover()`：对“state 有 Plan、tree 缺 experiment”的
    崩溃窗口做重建——从 `context_ref`/`plan_input` 和幂等 worktree 重建
    RUNNING 实验记录并写回 tree，避免丢失已投入的 Plan。
  - `recovery.py`：SEARCH Plan 若仍无对应 experiment，视为孤儿 Plan 丢弃，
    不再保留到 settle 崩溃（与 TS `recovery.ts` 对齐）。
- 验证：
  - `test_recovery.py` 更新并通过（7 passed）；
  - 手工异步脚本验证 `start_plan` 落 tree、删除 experiment 后 `recover()`
    可重建 RUNNING 实验并写回、纯 `Recovery.reconcile` 丢弃孤儿 Plan。

## 追加：合并 feature/survey-overhaul + 简化
- 分析 `feature/survey-overhaul`（54 commits，84 files，+12.7k 行）：核心是论文语料/引用核验、survey bench 与文档、plan/fork 修复。
- 合并到 main：解决 8 个冲突文件，保留 main 的断点续传/heartbeat/Kaggle handoff/任务理解，同时接入 feature 的 corpus_tools、citation verification、plan_tools。
- 简化：
  - `agent_turn_runner.py`：去掉重复的本地 `_read_eval_handoff`（改用 `supervisor.experiment.read_eval_handoff`），去掉冗余 `_literature_handoff_text`（语料提示已由 `_corpus_block` 提供）。
  - `runtime.py`：去掉 `start()` 中重复的 `_start_survey()` 调用。
  - `state.py`：`load()` 先迁移 `corpus_ideation_done` 再剥离未知键，保证旧状态迁移不丢。
  - 测试同步：`test_agent.py` 的 anthropic 断言改为 ValueError（当前不支持 anthropic）。
- 验证：`python -m pytest test/unit -q` = 1581 passed, 2 skipped, 1 failed（仅缺 `kagglehub` 可选依赖，与本次改动无关）；`test/unit/research + idea_generation + test_agent` = 492 passed。

## 追加：merge 后第二轮简化（能泛化就泛化）
- `paper_rag/tool.py`：
  - 抽出 `PaperRagTool`（artifact store + retrieval session）与 `PaperEmbeddingTool`（再加 embedder），7 个工具类去掉重复 `__init__`。
  - `_clamp_positive` 统一 `resolve_top_k` / `resolve_max_papers`；`_nonempty_str_list` / `_optional_str_list` 统一 chunk_ids/keywords/paper_ids 校验。
- `paper_scout/tool.py`：抽出 `PaperScoutTool` 基类，两个工具去掉重复 `__init__`。
- `bench/query_sets.py`：`_load_packaged` 统一 `load_query_set` / `load_recall_set` 的重复读取逻辑。
- `agent_turn_runner.py`：
  - `_tools_with_kaggle` 统一 General / Kaggle-handoff 的工具表构造；
  - `_collect_handoff_texts` 去掉只剩一个来源的循环；
  - 移除未使用的 `ArtifactStore` import。
- 验证：`pyflakes src/athena` = 0；`compileall` = 0；`pytest test/unit -q` = 1581 passed, 2 skipped, 1 failed（仅缺可选依赖 `kagglehub`，与本次改动无关）。

## 追加：删除更多 _ 辅助函数（内联单次调用的薄封装）
- `agent_turn_runner.py`：内联 `_interrupt_agent`、删除 `_lane_kwargs`（直接传 `handoff_texts` / `profile` 参数）。
- `supervisor/prepare.py`：内联 `_workspace_output` 的异常改写。
- `supervisor/scheduler.py`：内联 `_is_ready` 一行判据。
- `supervisor/ranker.py`：内联 `_settled_hypotheses` 列表推导。
- `supervisor/experiment.py`：内联 `_better` 方向比较。
- `supervisor/events.py`：内联 `_utf8_prefix`，并复用 `encoded` 避免重复编码。
- `supervisor/recovery.py`：内联 `_has_final_baseline`。
- 测试同步：`test_runtime_ideators.py` 的 lane 桩接受新增的 `handoff_texts` 关键字。
- 验证：`pyflakes src/athena` = 0；`compileall` = 0；`pytest test/unit -q` = 1581 passed, 2 skipped, 1 failed（仅缺可选依赖 `kagglehub`）。

## 追加：合并代码整理——SurveyReport/ScoutStats 去冗余
- `SurveyReport` 删除与 `ScoutStats` 重复的 9 个字段：`scout_pool`、`scout_retained`、`scout_dropped_no_source`、`boundary_tier`、`boundary_reranked`、`affinity_calls`、`affinity_failures`、`scout_busy`、`facets`、`facet_coverage`，改为持有 `scout: ScoutStats` 一个字段（实际删 10 个）。
- `SurveyPipeline._read_scout_result` 不再逐字段复制 scout 统计；`report.py` / `survey/tool.py` 改从 `report.scout` 读取。
- `ScoutStats` 删除与 `pool_size` 恒等的 `scored_papers`。
- `PaperScoutAgent._finish`：
  - `dropped_no_source` 改为 `len(eligible) - len(contenders)`，删掉对 `has_retrievable_source` 的逐篇调用与 import。
  - action 计数三遍推导合并为一趟循环。
- 测试同步：`FakeScoutAgent` 填充 `pool_size` / `retained_papers`，断言改用 `report.scout.*`。
- 验证：`pyflakes src/athena` = 0；`compileall` = 0；`pytest test/unit -q` = 1581 passed, 2 skipped, 1 failed（仅缺可选依赖 `kagglehub`）。

## 追加：合并相似工具类
- `core/tool.py`：新增 `StackTool(BaseTool)`，统一"只注入一个 stack"的工具底座。
- `kaggle/tool.py`：9 个 Kaggle 工具类不再各自定义 `__init__`，统一继承 `StackTool`；删除 `KaggleTool` 中间层与重复的 `stack` 存储。
- `research/survey/tool.py`：`PaperSurveyTool` 改为继承 `StackTool`，删除自己的 `__init__` 与 `TYPE_CHECKING` 依赖。
- 验证：`pyflakes src/athena` = 0；`compileall` = 0；`pytest test/unit -q` = 1581 passed, 2 skipped, 1 failed（仅缺可选依赖 `kagglehub`）。

## 追加：继续合并相似类与重复 schema
- `agents/orchestration.py`：
  - `_FollowupTool` 并入 `_RuntimeTool` 基座（`agent_id` 置空），删除独立 `__init__`。
  - 抽出 `_MESSAGE_INPUT_SCHEMA`，send/followup 共用同一输入 schema。
- `kaggle/tool.py`：抽出 `_COMPETITION_LIST_SCHEMA`，list_notebooks / list_discussions 共用。
- 验证：`pyflakes src/athena` = 0；`compileall` = 0；`pytest test/unit -q` = 1581 passed, 2 skipped, 1 failed（仅缺可选依赖 `kagglehub`）。

## 追加：源码文件逐项简化（batch）
- `bench/known_item.py`：`distinct_papers` 改为 `dict.fromkeys` 保序去重。
- `paper_scout/backends.py`：Semantic Scholar 的 search/references 共用 `_get_json`，去掉重复的 HTTP 错误检查与 JSON 解析。
- `paper_source/fetcher.py`：`pdf_hint_urls` 改为 `dict.fromkeys` 保序去重。
- `paper_rag/interfaces.py` + `__init__.py`：删除无实现、无调用点的 `ChunkContextualizer` 契约。
- 验证：`pyflakes src/athena` = 0；`compileall` = 0；`pytest test/unit -q` = 1581 passed, 2 skipped, 1 failed（仅缺可选依赖 `kagglehub`）。
