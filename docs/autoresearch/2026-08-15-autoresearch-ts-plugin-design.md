# AutoResearch 框架与 TS 插件设计

日期：2026-08-15
状态：设计文档
包形态：独立 `@athena/autoresearch` 包 + 独立 `autoresearchPlugin`
前置：`../superpowers/specs/2026-08-14-athena-dsh-plugin-design.md`（Athena 作为 DSH 插件）
配套：`2026-08-15-hypothesis-local-pool-design.md`（HypothesisPool 设计）

---

## 0. 2026-08-20 执行基线

本设计的包形态和 DSH 插件方向继续有效，但首个实现以
[`2026-08-20-autoresearch-ml-control-plane-design.md`](2026-08-20-autoresearch-ml-control-plane-design.md)
为执行基线：

- DSH 已有的 Agent、Subagent、Goal、Workflow、Tools、持久化、沙箱和模型路由
  全部复用，AutoResearch 不再实现对应基础设施；
- 首版只注册一个主要 `AutoResearchService`，负责 handoff 推进、精简状态、动态
  rubric、自主研究循环和关键 gate；
- 本文的阶段与 Paper Engine 规则作为领域语义保留，细粒度 Service/API 骨架不要求
  在 ML v1 一次性实现；
- `candidate-to-paper-handoff/` 和 `records-paper-handoff/` 是首版流程主体；
- 当前 Python Athena 暂作实验 Provider，AutoResearch Core 不依赖其私有状态类型；
- 实施顺序为控制平面、质量闭环、自主迭代、最后通用化。

若本文的实现结构与 2026-08-20 设计冲突，以后者为准。

## 1. 背景与目标

AutoResearch 是一个**端到端研究自动化框架**：从 idea generation 到 paper generation。

- 其 **Experiment 子集 = 当前 Athena 工作流**（PREPARE → SEARCH → VALIDATE），但 Experiment 阶段允许被重新设计/替换。
- 交付物为**完整 LaTeX/PDF 论文**，可选择 ICLR / ICML 等 ML 顶会 LaTeX 模板；同时支持**无 LaTeX 过程的 Markdown draft**。
- 论文生成支持 **Overleaf 接入**（LLM 直接编辑 LaTeX 文件）与**本机 TeX 编译器**两条路径。
- 全自动运行，但必须有预算与质量闸。
- 阴性结果保留并写入 limitations / negative results。
- 本次只写设计文档，不实现代码。

## 2. 需求快照（来自需求确认）

| # | 决策 |
|---|---|
| D1 | 独立 `@athena/autoresearch` 包 + 独立 `autoresearchPlugin` |
| D2 | 端到端阶段：Idea Generation → Experiment（Athena 子集，可重设计）→ Paper Writing → Paper Refinement/Quality Gate → Artifact Packaging |
| D3 | Hypothesis 本地池：独立 `HypothesisPool` 服务 + `hypothesis_pool.json` |
| D4 | 状态机：新增 `AutoResearchState` + 独立 state 文件；Experiment 内部委托 `ResearchState` |
| D5 | 论文交付物：完整 LaTeX/PDF；ML 顶会模板可选；支持 Markdown draft |
| D6 | 论文实现：Overleaf 走官方 API，LLM 直接编辑 LaTeX；可选本机 TeX 编译器；无 LaTeX 过程时转 Markdown draft |
| D7 | 门禁：全自动但有预算与质量闸 |
| D8 | 阴性结果保留并写入 limitations / negative results |
| D9 | 模板获取：每次启动用 `curl` 从会议官网拉取 LaTeX 模板；失败回退上次缓存 |
| D10 | 成本预算：首版只做 token 预算，不接计费（`max_cost` 不做） |
| D11 | LaTeX 编译失败自修复：不做轮次限制，只受项目总时间限制；每次修复返回控制台/编译器输出 |
| D12 | 本次交付：只写设计文档 |

## 3. 总体架构

```
┌────────────────────────────────────────────────────────────────┐
│                      AutoResearchSupervisor                      │
│   (单写者；消费 AutoResearchState + HypothesisPool；发布事件)      │
└──────────────┬─────────────────────────────────────────────────┘
               │
   ┌───────────┼──────────────────────────────────────────────┐
   │           ▼                                              │
   │  Stage: INTAKE → IDEATION → EXPERIMENT → WRITING          │
   │                → REFINEMENT → PACKAGING → COMPLETED       │
   └───────────┬──────────────────────────────────────────────┘
               │
   ┌───────────┼──────────────────────────────────────────────┐
   │           │                                              │
   │  IdeaGeneration    AthenaExperimentEngine   PaperEngine    │
   │  - gap mining      - PREPARE (Athena)       - LaTeXBuilder │
   │  - candidate gen   - SEARCH  (Athena)       - OverleafConn │
   │  - gates/ranking   - VALIDATE(Athena)       - MarkdownDraft│
   │  - HypothesisPool  - ResearchTree/State     - TemplateKit  │
   └────────────────────────────────────────────────────────────┘
```

### 3.1 三层关系

| 层 | 职责 | 现有/新增 |
|---|---|---|
| AutoResearch 编排层 | 阶段推进、预算/门禁、状态机、事件发布 | 新增 `@athena/autoresearch` |
| Athena 子集（Experiment 引擎） | PREPARE→SEARCH→VALIDATE、ResearchTree/State、Scheduler、Evaluator | 复用 `@athena/dsh` 研究服务与 Python 工作流 |
| Paper 引擎 | 模板选择、LaTeX 编辑、编译、审稿质量门 | 新增（可接 Python paper_* 能力） |

### 3.2 插件形态

独立包 `@athena/autoresearch` 导出一个 `autoresearchPlugin(ctx)`，它依赖并复用 `@athena/dsh` 已注册的研究服务（`researchTree / researchState / researchScheduler / researchSupervisor / researchStore / researchGit / researchEvaluator / researchScriptRunner`），并新增：

| 概念服务 | 说明 |
|---|---|
| `hypothesisPool` | 独立本地池，见配套设计 |
| `autoResearchState` | AutoResearch 阶段状态，独立 state 文件 |
| `autoResearchSupervisor` | 阶段编排单写者 |
| `paperComposer` | 从证据包写 LaTeX / Markdown 草稿 |
| `paperReviewer` | 论文质量门禁（证据可追溯、模板合规、阴性结果完整） |
| `latexBuilder` | 本机 TeX 编译（latexmk / tectonic 等） |
| `overleafConnector` | Overleaf 项目同步、编译、日志回传 |
| `templateKit` | ICLR/ICML 等模板资产管理与选择 |

包依赖：`@athena/autoresearch` → `@athena/dsh`（服务复用）→ `@athena/research` / `@athena/core`。`autoresearchPlugin` 只在 DSH 环境内组合，不直接依赖 Python 运行时。

## 4. 阶段设计

### Stage 0 — INTAKE（任务定格）

输入：研究任务、领域、目标指标方向、预算上限、论文模板选择（`iclr2026` / `icml2026` / 通用 `markdown`）。
输出：`AutoResearchState` 初始化，`PaperSpec` 冻结。

`PaperSpec` 冻结字段：

```jsonc
{
  "template": "iclr2026",              // 或 "icml2026" / "markdown" / "plain_latex"
  "venue": "ICLR 2026",
  "main_language": "latex",            // latex | markdown
  "latex_via": "overleaf",             // overleaf | local | none（none 仅 Markdown draft）
  "local_compiler": "latexmk",         // latexmk | tectonic | pdflatex
  "title": "...",
  "authors": [],
  "abstract": "由 Paper Writing 生成"
}
```

### Stage 1 — IDEATION（Idea Generation）

职责：从文献/空白挖掘到候选假设，进入 `HypothesisPool`。

流水线：

1. **空白挖掘**：复用 `idea_generation/mine_research_gaps`（或 Python `paper_rag` 检索能力）产出 `GapCandidate`。
2. **候选生成**：复用 `generate_candidates` 的五策略并行（类比迁移 / 机制推演 / 反直觉 / 约束松弛 / 边界外推），或 SEARCH 内实时 EDA-grounded Ideator。
3. **门禁**：`pre_gate → 审阅 → hard_gate/light_hard_gate`；REJECT 候选入池标记 `REJECTED` 并附拒绝理由。
4. **排序**：存活候选经 pairwise Elo（`HypoPriList`）排序，写入池 `QUEUED`。
5. **预算**：`max_ideas` 与 `max_tokens` 双重上限；达到上限后阶段收敛。

阶段完成条件：池中 `QUEUED` 候选数 ≥ 1，或预算耗尽（记录 `IDEATION_EXHAUSTED`）。

### Stage 2 — EXPERIMENT（Athena 子集）

职责：用实验把池中候选变成可信证据。

默认模式：**Athena 工作流子集**，阶段委托给 `ResearchState` 的 `PREPARE → SEARCH → VALIDATE`。

```
AutoResearch EXPERIMENT
  ├─ PREPARE  → baseline + frozen evaluator + EDA worktree
  ├─ SEARCH   → 从 HypothesisPool.queued() 供给候选（或由 Scheduler 从树 pending 供给）
  └─ VALIDATE → 冻结 SOTA 的最终评估
```

关键适配点（概念）：

- **候选供给**：`Experiment` 阶段启动时，把池 `QUEUED` 条目写入 `ResearchTree`（等价 `register_hypotheses`）；结算后按配套设计回写池。
- **Experiment 可重设计**：允许 `experiment_strategy` 配置：
  - `athena_rolling`：当前 Athena 的 rolling Scheduler（默认，行为不变）。
  - `mcts`：使用 `../dsh_docs/mcts-tree-search-plan.md` 的 MCTS 调度器（未来）。
  - `pool_greedy`：直接按 `HypothesisPool` 的池状态 + 现有 `Selector` 排序供给。
- **结算回写池**：WIN → `SUPPORTED`；DRAW/LOSS → `REFUTED`；无证据 → `INCONCLUSIVE`。
- **预算**：`max_experiments`（映射 `search_limit`）与全局 `max_tokens`；首版不做 `max_cost`。

阶段完成条件：搜索预算耗尽或人工触发 VALIDATE，且 `ResearchState.phase == COMPLETED`。

### Stage 3 — WRITING（Paper Writing）

职责：从实验证据与假设池写完整 LaTeX/PDF 论文（或 Markdown draft）。

输入：

- `ResearchTree` 的 SOTA 路径 + 全部已结算实验。
- `HypothesisPool.export_for_paper()`（含 `PROMOTED_TO_PAPER` 候选与 `negative_results()`）。
- `PaperSpec`（模板、venue、编译方式）。

输出物（LaTeX 模板路径）：

```
paper/<run_id>/
  main.tex                 # 模板入口，只改 title/authors/abstract/input
  sections/
    introduction.tex
    related_work.tex
    method.tex
    experiments.tex
    results.tex
    limitations.tex        # 阴性结果与局限
    conclusion.tex
  figures/
  tables/
  bibliography.bib
  paper_draft.md           # 始终生成的 Markdown 草稿
```

编辑方式：

- **LLM 直接编辑 LaTeX 文件**：Paper Writer Agent 的工具绑定 `paper/<run_id>/`，按模板约束逐 section 写入；模板 class/style 文件只读。
- **Overleaf 模式**：`OverleafConnector` 把本地 `paper/<run_id>/` 同步为 Overleaf 项目，LLM 编辑后由 Overleaf 编译，取回 PDF 与日志。
- **本机 TeX 模式**：`LatexBuilder` 在本地执行 `latexmk -pdf`（或 tectonic），产出 PDF 与日志。
- **无 LaTeX 过程**：只生成/更新 `paper_draft.md`，跳过编译；后续可随时升级到 LaTeX。

写作纪律（写入 Prompt/质量闸）：

1. 每个实验声明必须可追溯到 `experiment_id` 与 `eval.primary`，不得编造指标。
2. 每个图/表必须引用 `ResearchTree` 的 artifact 引用或 `HypothesisPool` 证据。
3. 阴性结果必须写入 `limitations.tex` 或模板对应 section。
4. 不使用模板以外的 class/style 文件；不改模板宏。

### Stage 4 — REFINEMENT / Quality Gate（论文修订与质量闸）

职责：对初稿做自动审稿并修订，直到通过或预算耗尽。

质量闸（每轮打分）：

| 检查项 | 判定 |
|---|---|
| `evidence_traceable` | 所有报告数字可在 `ResearchTree`/`HypothesisPool` 中找到；无幻觉指标 |
| `template_compliant` | 满足 ICLR/ICML 模板限制（页数、section 命名、匿名等） |
| `negative_results_complete` | 所有已执行但未提升的实验在 limitations 有记录 |
| `claim_consistency` | 正文结论与 `EvalResult` 方向一致 |
| `compile_ok` | LaTeX 模式必须编译通过；Markdown 模式跳过 |
| `novelty_statement` | 与相关工作区分，不夸大贡献 |

修订循环：

```
draft → paperReviewer 打分 → 全过 → 进入 PACKAGING
      → 不过 → 把逐项意见返回 paper writer → 修订 → 再评
      → max_paper_rounds 耗尽 → 冻结当前最优版本 + 记录未过项
```

编译失败的修复是**独立循环**，不走 `max_paper_rounds`：

```
compile_ok=false → latexBuilder/Overleaf 返回控制台输出 → 把编译日志原文喂回 Paper Writer
      → 修订 LaTeX → 重新编译 → 直到 compile_ok=true 或项目总时间预算耗尽
```

- **不做轮次限制**；只受项目总时间限制（`project_time_limit`）。
- 每次编译尝试的**控制台/编译器输出必须作为事件返回**给调用方（`paper_compile_output` 事件，含 stdout/stderr/log 原文），不得吞掉。
- 时间耗尽仍编译不过：冻结当前源文件 + 完整编译日志 + `compile_ok=false` 标记，并在最终状态中可见。

### Stage 5 — PACKAGING（Artifact Packaging）

产出：

- `paper.pdf`（LaTeX 模式；由 Overleaf 或本机编译器编译）。
- `paper_draft.md`（始终）。
- `paper_sources/`（LaTeX 源文件 + figures + bibliography）。
- `experiment_evidence.json`：论文中每个 claim → `experiment_id`/`artifact_ref`/`pool_id` 的可追溯清单。
- `reproducibility_bundle/`：SOTA 实验 diff、评估器引用、数据脚本引用、运行日志摘要。

## 5. AutoResearchState 状态机

### 5.1 顶层阶段

```
INTAKE → IDEATION → EXPERIMENT → WRITING → REFINEMENT → PACKAGING → COMPLETED
   │         │           │            │            │             │
   └─────────┴───────────┴────────────┴────────────┴──────► WAITING / STOPPED / FAILED
```

- `WAITING`：预算耗尽或质量闸不过且 `auto_resume=false`，等待人工补充预算/决策。
- `STOPPED`：人工停止。
- `FAILED`：不可恢复基础设施错误。

### 5.2 持久化

```jsonc
{
  "version": 1,
  "run_id": "ar_1a2b3c4d5e6f",
  "phase": "REFINEMENT",                 // 顶层阶段
  "status": "RUNNING",                   // RUNNING/WAITING/STOPPED/FAILED/COMPLETED
  "paper_spec": { /* Stage 0 冻结 */ },
  "stage": {
    "phase": "REFINEMENT",
    "round": 2,
    "budget": { "max_rounds": 5, "used_rounds": 2 },
    "gate_result": { /* 最近一次质量闸 */ }
  },
  "experiment_substate": {               // 委托给 ResearchState 的状态快照引用
    "research_phase": "COMPLETED",
    "research_status": "COMPLETED",
    "state_ref": ".athena/state.json"
  },
  "budgets": {
    "max_ideas": 20,
    "max_experiments": 10,
    "max_paper_rounds": 5,
    "max_tokens": 0,                     // 0 = 不限；首版只做 token 预算，不接计费
    "project_time_limit": "12h"          // 项目总时间限制；编译自修复循环以此为唯一上限
  }
}
```

- 文件路径：`.athena/autoresearch/state.json`。
- 单写者：`AutoResearchSupervisor`。
- `experiment_substate` 不复制 `ResearchState`，只引用 `.athena/state.json` 并记录关键快照，避免双状态漂移。

## 6. 预算与质量闸（D7）

### 6.1 预算维度

| 维度 | 作用阶段 | 耗尽行为 |
|---|---|---|
| `max_ideas` | IDEATION | 停止生成，进入 EXPERIMENT |
| `max_experiments` | EXPERIMENT | 触发 Athena 的 search_limit / VALIDATE 决策 |
| `max_paper_rounds` | WRITING/REFINEMENT | 冻结当前最优版本并标记未过项（内容质量修订用） |
| `max_tokens` | 全局 | 阶段间检查，超限置 WAITING（首版唯一成本预算） |
| `project_time_limit` | 全局 | 总时间上限；LaTeX 编译失败自修复只受此限制，无轮次上限 |

> 首版不做 `max_cost`：不接模型计费数据，只按 token 预算与时间预算控制成本。

### 6.2 自动闸

- 每阶段结束执行 `gate`，通过才进入下一阶段；不通过且预算未耗尽 → 修订/重新生成；预算耗尽 → `WAITING`。
- 质量闸绝不静默放行：任何不通过都必须有可读的 `blocking_factor` 与证据。

## 7. 论文模板与编译路径（D5/D6）

### 7.1 模板选择与获取

- `templateKit` 维护可用模板：`iclr2026`、`icml2026`、`neurips2026`（占位）、`plain_latex`、`markdown`。
- INTAKE 阶段选择并冻结进 `PaperSpec`。
- **每次 AutoResearch 启动时**，`templateKit` 用 `curl` 从会议官网/官方模板页拉取所选模板压缩包：
  - 成功：解压到 `templates/<venue>/<version>/`，记录 `fetched_at` 与来源 URL。
  - 失败：回退到上次缓存 `templates/<venue>/<version>.cache/`；无缓存则 INTAKE 置 `WAITING` 并返回控制台错误。
- 模板资产只读；LLM 只编辑 `main.tex` 与 `sections/*.tex`、`bibliography.bib`。

### 7.2 三条论文生成路径

| 路径 | 编辑方式 | 编译 | 输出 |
|---|---|---|---|
| `overleaf` | 通过 **Overleaf 官方 API** 创建/同步项目，LLM 经工具编辑 LaTeX | Overleaf 云端编译 | PDF + 日志回传 |
| `local` | LLM 编辑本地 `paper/<run_id>/` LaTeX | `latexmk` / `tectonic` 本地编译 | PDF + 日志 |
| `markdown` | LLM 只编辑 `paper_draft.md` | 无 | Markdown draft |

无论哪条路径，**Markdown draft 始终生成**，作为低成本中间产物与可读快照。

### 7.3 Overleaf 连接器

- 接入方式：**Overleaf 官方 API**。
- 配置：`OVERLEAF_API_TOKEN`（+ 可选 `OVERLEAF_API_BASE_URL`），通过环境变量注入，不落 state 文件。
- 能力：创建项目、上传/同步文件、触发云端编译、获取 PDF、拉取编译日志（stdout/stderr 原文）。
- 失败降级：Overleaf 不可用时回退 `local` 编译；本机无 TeX 时回退 `markdown`。
- 每次编译尝试的日志都作为 `paper_compile_output` 事件返回控制台/订阅方。

## 8. 与 Athena 子集的关系（D2 重设计空间）

- 默认：`AutoResearch.EXPERIMENT = Athena(PREPARE→SEARCH→VALIDATE)`，契约不变。
- 重设计点（不改变默认行为，作为可插拔策略）：
  1. **候选来源**：从 `HypothesisPool.queued()` 供给，而非只能从树 `pending_hypotheses()`。
  2. **搜索策略**：允许 `rolling`（默认）与 `mcts`（见 MCTS 计划）切换。
  3. **动态 EDA**：继续作为 SEARCH 内 Ideator 的补充信息回路。
  4. **结算语义**：DRAW/LOSS 仍需结算并回写池，作为阴性结果进入论文。
- 这些重设计若实现，必须保持 `research_strategy="athena_rolling"` 时与当前 Athena 行为逐字节一致。

## 9. 概念服务与模型可见工具

| 工具名（概念） | 作用 |
|---|---|
| `autoresearch_run` | 启动/恢复 INTAKE→…→PACKAGING 全流程 |
| `autoresearch_status` | 读顶层阶段、预算、质量闸、池统计、SOTA |
| `autoresearch_pause` / `autoresearch_resume` | 人工暂停/继续 |
| `paper_set_template` | 切换/冻结论文模板（需在 WRITING 前） |
| `paper_compile` | 手动触发 LaTeX 编译并回传日志 |
| `paper_draft` | 导出当前 Markdown 草稿 |

以上工具是否注册为 DSH 动态工具、还是 AutoResearch 自己的服务方法，留在实现阶段按 `@athena/dsh` 的实际 `ctx.tools.register` 形态决定。

## 10. 事件与可观测性

- 复用 `RuntimeEvents` 风格：阶段开始/结束、门禁结果、预算消耗、编译日志、论文版本。
- 每个论文版本生成一个 `paper_version` 事件，携带 `paper_draft_ref` / `paper_pdf_ref` / `gate_result`。
- 所有大对象只经 ArtifactRef 传递，不塞事件 payload。

## 11. 验收标准（未来实现时）

1. 设计文档落地后，`docs/README.md` 可索引到本设计与 HypothesisPool 设计。
2. 实现阶段：`@athena/autoresearch` 独立包存在，`autoresearchPlugin` 可复用 `@athena/dsh` 研究服务。
3. `hypothesis_pool.json` 与 `autoresearch/state.json` 可崩溃恢复；从树可重建池。
4. 默认 `athena_rolling` 模式下，Experiment 阶段输出与当前 Athena 一致（对拍测试）。
5. 论文路径：
   - `markdown` 模式始终产出 `paper_draft.md`。
   - `local` 模式在本机有 TeX 时产出 `paper.pdf`。
   - `overleaf` 模式通过官方 API 在配置有效时产出云端编译 PDF。
6. 质量闸拒绝时必须给出 `blocking_factor` 与证据，不得静默通过。
7. 阴性结果完整性：所有已结算非 WIN 假设均出现在 limitations/negative results 或显式归档。
8. 编译自修复：`compile_ok=false` 时循环修复直到 `project_time_limit` 耗尽；每次尝试返回控制台/编译器输出事件。

## 12. 本设计文档之外的开放项

1. **模板来源与授权**：ICLR/ICML 官方模板页 URL 白名单、抓取频率与授权边界。
2. **Experiment 重设计触发条件**：何时从 `athena_rolling` 切到 MCTS 或 pool_greedy。
3. **项目时间限制的默认值**：`project_time_limit` 按任务类型默认多少、如何被用户覆盖。
4. **Overleaf 官方 API 的速率限制与重试策略**：由实现阶段对拍官方文档确认。
5. **Markdown draft 的格式规范**：是否需要固定 section 模板（用于未来升级 LaTeX）。
