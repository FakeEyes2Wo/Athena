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
