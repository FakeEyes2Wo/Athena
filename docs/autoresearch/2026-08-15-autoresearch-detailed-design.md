# AutoResearch 详细设计（实现级）

日期：2026-08-15
状态：历史实现级设计（领域规则保留，代码结构已由 2026-08-20 设计精简）
上游：`2026-08-15-autoresearch-ts-plugin-design.md`（概念）、`2026-08-15-hypothesis-local-pool-design.md`（池模型）
包：`@athena/autoresearch`

---

## 0. 2026-08-20 实现覆盖说明

本文定义的阶段语义、预算、质量约束、Paper Engine 行为和验收场景继续作为参考；
包结构、zod 模型数量、Service 数量、事件层和通用 API 不再是 ML v1 的实现要求。

ML v1 以
[`2026-08-20-autoresearch-ml-control-plane-design.md`](2026-08-20-autoresearch-ml-control-plane-design.md)
为准，只实现一个主要 `AutoResearchService`、精简状态、ML profile 和 Athena 薄适配，
其余流程复用 DSH 与现有 handoff。不要按本文逐项建立类、interface 或事件 DTO。

## 1. 本文目的

把概念设计细化到可实现的程度：包结构、运行时目录、zod 模型、服务 API、阶段伪代码、插件注册、事件、恢复、测试与验收。本文不写实现代码，只定义契约与算法。

## 2. TS 包结构

```
athena_ts/packages/athena-autoresearch/
  package.json                     # name: @athena/autoresearch
  tsconfig.json
  src/
    index.ts                       # autoresearchPlugin(ctx, config) + public exports
    schemas/
      state.ts                     # AutoResearchState / StageState / Budgets
      paper-spec.ts                # PaperSpec
      hypothesis-pool.ts           # PooledHypothesis / PoolStatus / pool file schema
      events.ts                    # event payload schemas
    services/
      hypothesis-pool.ts           # HypothesisPool
      state-store.ts               # AutoResearchStateStore（原子读写）
      template-kit.ts              # curl 拉取 + 缓存 + 选择
      overleaf-connector.ts        # Overleaf 官方 API
      latex-builder.ts             # 本机 TeX 编译
      paper-composer.ts            # LaTeX / Markdown 草稿生成
      paper-reviewer.ts            # 论文质量闸
      packaging.ts                 # 产物打包与可追溯清单
    supervisor/
      auto-research-supervisor.ts  # 阶段状态机单写者
      stages/
        intake.ts
        ideation.ts
        experiment.ts
        writing.ts
        refinement.ts
        packaging-stage.ts
    tools.ts                       # DSH 模型可见工具定义
    plugin.ts                      # cordis 注册与生命周期
  test/
    ...                            # 见 §12
```

依赖：`@athena/core`、`@athena/research`、`@athena/dsh`（仅类型/运行时服务消费）、`zod`、`@deepseek-ai/*`（DSH 环境内）。

## 3. 运行时目录

```
<project_root>/.athena/autoresearch/
  state.json                       # AutoResearchState
  hypothesis_pool.json             # HypothesisPool
  templates/
    <venue>/
      current/                     # 本次启动 curl 获取并解压
      cache/                       # 上次成功缓存（启动失败时回退）
      fetch.json                   # {url, fetched_at, sha256}
  paper/<run_id>/
    main.tex
    sections/*.tex
    figures/
    tables/
    bibliography.bib
    paper_draft.md                 # 始终生成
  packaging/<run_id>/
    paper.pdf
    paper_sources.zip
    experiment_evidence.json
    reproducibility_bundle/
  logs/
    compile/                       # 每次编译的 stdout/stderr 原文
```

## 4. 核心模型（zod 契约）

### 4.1 `AutoResearchState`

```ts
const AutoResearchPhase = z.enum([
  "INTAKE", "IDEATION", "EXPERIMENT", "WRITING",
  "REFINEMENT", "PACKAGING", "COMPLETED",
])
const AutoResearchStatus = z.enum([
  "RUNNING", "WAITING", "STOPPED", "FAILED",
])

const StageState = z.object({
  phase: AutoResearchPhase,
  round: z.number().int().min(0),
  used_rounds: z.number().int().min(0),
  budget: z.record(z.string(), z.unknown()),   // 阶段内预算使用计数
  gate_result: z.unknown().optional(),         // 最近一次质量闸结果
})

const AutoResearchStateSchema = z.object({
  version: z.literal(1),
  run_id: z.string().regex(/\S/),
  phase: AutoResearchPhase,
  status: AutoResearchStatus,
  paper_spec: PaperSpecSchema,
  stage: StageState,
  budgets: BudgetsSchema,
  experiment_substate: z.object({
    research_phase: z.string(),
    research_status: z.string(),
    state_ref: z.string(),                     // 相对项目根，默认 .athena/state.json
  }).optional(),
  started_at: z.string(),
  updated_at: z.string(),
  deadline: z.string(),                        // started_at + project_time_limit
  stopped_by: z.string().optional(),
})
```

### 4.2 `Budgets`

```ts
const BudgetsSchema = z.object({
  max_ideas: z.number().int().min(1),
  max_experiments: z.number().int().min(1),
  max_paper_rounds: z.number().int().min(1),
  max_tokens: z.number().int().min(0),         // 0 = 不限；首版唯一成本预算
  project_time_limit: z.string(),              // ISO-8601 duration，如 "PT12H"
  tokens_used: z.number().int().min(0),
})
```

### 4.3 `PaperSpec`

```ts
const PaperSpecSchema = z.object({
  template: z.enum(["iclr2026", "icml2026", "neurips2026", "plain_latex", "markdown"]),
  venue: z.string(),
  main_language: z.enum(["latex", "markdown"]),
  latex_via: z.enum(["overleaf", "local", "none"]),
  local_compiler: z.enum(["latexmk", "tectonic", "pdflatex"]).optional(),
  title: z.string(),
  authors: z.array(z.string()),
  abstract: z.string().default(""),
  overleaf_project_id: z.string().optional(),
})
```

### 4.4 `PooledHypothesis`

见 `2026-08-15-hypothesis-local-pool-design.md` §3；zod 化时额外约束：

- `pool_status` 枚举与状态机迁移函数放 `schemas/hypothesis-pool.ts`。
- `pool_id` 与内嵌 `hypothesis.id` 必须相等（`superRefine`）。
- `updated_at` 每次 upsert 强制刷新。

## 5. 服务契约

### 5.1 `HypothesisPool`

```ts
class HypothesisPool {
  constructor(opts: {
    path: string                    // .athena/autoresearch/hypothesis_pool.json
    tree: ResearchTree
  })
  load(): void
  save(): void                      // atomicWriteJson
  reconcile(): void                 // 从 ResearchTree 对账（见池设计 §4）
  upsert(record: PooledHypothesis): void
  get(poolId: string): PooledHypothesis
  list(status?: PoolStatus): PooledHypothesis[]
  queued(): PooledHypothesis[]
  supported(): PooledHypothesis[]
  refuted(): PooledHypothesis[]
  inconclusive(): PooledHypothesis[]
  promotedToPaper(): PooledHypothesis[]
  negativeResults(): PooledHypothesis[]
  recordGateRejection(poolId, reason): void
  recordSettlement(poolId, outcome): void
  recordPaperPromotion(poolId, paperRefs, sectionIds): void
  archive(poolId): void
  supersede(ancestorPoolId): void
  findDuplicates(text: string, threshold: number): PooledHypothesis[]
  exportForPaper(): PaperEvidenceBundle
}
```

`PaperEvidenceBundle`：

```ts
{
  sota: ExperimentEvidence | null
  supported: ExperimentEvidence[]
  negative: ExperimentEvidence[]
  queued: PooledHypothesis[]        // 未实验但可写入 future work
}
```

### 5.2 `AutoResearchStateStore`

```ts
class AutoResearchStateStore {
  constructor(path: string)
  load(): AutoResearchState
  save(state: AutoResearchState): void
  transition(state, phase): AutoResearchState   // 校验合法迁移
  setStatus(state, status): AutoResearchState
  recordStageResult(state, result): AutoResearchState
  tokensUsed(state): number
  addTokens(state, n): AutoResearchState
  deadlineReached(state): boolean
}
```

合法迁移矩阵：

```
INTAKE → IDEATION
IDEATION → EXPERIMENT | WAITING | FAILED
EXPERIMENT → WRITING | WAITING | FAILED
WRITING → REFINEMENT | WAITING | FAILED
REFINEMENT → PACKAGING | WAITING | FAILED
PACKAGING → COMPLETED | FAILED
任意非终态 → STOPPED（人工）
```

### 5.3 `TemplateKit`

```ts
class TemplateKit {
  constructor(opts: {
    cacheRoot: string               // .athena/autoresearch/templates
    fetch?: (url: string) => Promise<Buffer>   // 默认用 curl 子进程
  })
  async fetch(venue: TemplateVenue): Promise<TemplateFetchResult>
  select(spec: PaperSpec): TemplateDir   // 校验并返回 current/cache 目录
}
```

`fetch(venue)` 算法：

1. 从 venue → 官方模板 URL 白名单取 URL。
2. `curl -L --fail --silent --show-error <url> -o <tmp>.zip`；`curl` 的 stdout/stderr 原文进入日志。
3. 解压到 `templates/<venue>/current/`，计算 sha256，写 `fetch.json`。
4. 成功：把 `current/` 复制为 `cache/`。
5. 失败：若 `cache/` 存在则返回 `{dir: cache, source: "cache"}`；否则抛 `TemplateFetchError`，INTAKE 置 `WAITING` 并向控制台返回错误。

### 5.4 `OverleafConnector`

```ts
class OverleafConnector {
  constructor(opts: { apiToken: string; apiBaseUrl?: string })
  async healthCheck(): Promise<boolean>
  async createProject(name: string): Promise<string>   // project id
  async syncFiles(projectId: string, files: Map<string, Uint8Array>): Promise<void>
  async compile(projectId: string): Promise<CompileResult>
  async getPdf(projectId: string): Promise<ArtifactRef>
  async getLogs(projectId: string): Promise<CompileLog>
}
```

`CompileResult`：`{ ok: boolean; pdfRef?: ArtifactRef; log: CompileLog; consoleText: string }`。
`CompileLog`：`{ stdout: string; stderr: string; warnings: string[] }`。

失败降级：`healthCheck` 失败或 `compile` 抛网络错误 → 上层切换 `latex_via="local"`；本机无 TeX → 切换 `"none"`（仅 Markdown draft），并在状态中记录降级原因。

### 5.5 `LatexBuilder`

```ts
class LatexBuilder {
  constructor(opts: { compiler: "latexmk" | "tectonic" | "pdflatex" })
  async compile(projectDir: string): Promise<CompileResult>
}
```

- 子进程执行 `latexmk -pdf -interaction=nonstopmode -halt-on-error`（或 tectonic）。
- stdout/stderr 原文完整写入 `.athena/autoresearch/logs/compile/<run_id>-<ts>.log`，同时通过 `paper_compile_output` 事件返回控制台。
- 返回 `{ ok, pdfPath?, logPath, consoleText }`。

### 5.6 `PaperComposer`

```ts
class PaperComposer {
  constructor(opts: { templateKit: TemplateKit; latexBuilder: LatexBuilder; overleaf: OverleafConnector })
  async compose(input: PaperInput): Promise<PaperDir>
  async writeMarkdownDraft(input: PaperInput): Promise<ArtifactRef>
}
```

`PaperInput`：

```ts
{
  runId: string
  spec: PaperSpec
  tree: ResearchTree
  pool: HypothesisPool
  evidence: PaperEvidenceBundle
}
```

`compose` 顺序：

1. `templateKit.select(spec)` 得到模板目录。
2. 复制模板骨架到 `paper/<run_id>/`。
3. 由 Paper Writer subagent 逐 section 写 LaTeX；每次写文件后立即写 `paper_draft.md` 对应 section（Markdown 与 LaTeX 内容同步生成，保证草稿始终最新）。
4. 写完后调用 `latexBuilder` 或 `OverleafConnector` 做第一次编译。
5. 返回 `PaperDir`（含初稿与首次编译日志）。

### 5.7 `PaperReviewer`

```ts
class PaperReviewer {
  async review(input: ReviewInput): Promise<GateResult>
}
```

`ReviewInput`：`{ paperDir, evidence, spec, compileResult }`。
`GateResult`：`{ pass: boolean; blockingFactor?: string; itemScores: RubricItem[]; feedback: string[] }`。

质量闸实现顺序：

1. `compile_ok`：优先检查；LaTeX 模式不通过则进入编译修复循环（§8.4），不消耗 `max_paper_rounds`。
2. `evidence_traceable`：正则/结构化校验每个数字是否有 `experiment_id`/`artifact_ref` 支撑；不可支撑 → 不通过。
3. `template_compliant`：页数、section 命名、匿名化检查（由模板规则表驱动）。
4. `negative_results_complete`：`pool.refuted() ∪ pool.inconclusive()` 必须在 limitations 出现或显式 `ARCHIVED`。
5. `claim_consistency`：正文结论与 `EvalResult.primary` 方向一致。
6. `novelty_statement`：不夸大；贡献句与 evidence bundle 对齐。

### 5.8 `AutoResearchSupervisor`

```ts
class AutoResearchSupervisor {
  constructor(opts: {
    projectRoot: string
    stateStore: AutoResearchStateStore
    pool: HypothesisPool
    tree: ResearchTree
    researchSupervisor: FixedFlowSupervisor      // Athena 子集
    templateKit: TemplateKit
    overleaf: OverleafConnector
    latexBuilder: LatexBuilder
    composer: PaperComposer
    reviewer: PaperReviewer
    packager: PackagingService
    runIdeation: (count: number) => Promise<Hypothesis[]>   // 复用 DSH subagent / Python
    runPaperTurn: (task: string) => Promise<string>         // Paper Writer subagent
    publish: (event: AutoResearchEvent) => Promise<void>
  })
  async run(): Promise<void>
  async pause(): Promise<void>
  async resume(): Promise<void>
  async stop(): Promise<void>
}
```

## 6. 插件注册（DSH cordis）

`src/plugin.ts` 概念实现（非代码）：

```ts
export function autoresearchPlugin(config: AutoResearchConfig = {}) {
  return (ctx: Context): void => {
    const athena = ctx.get("researchStore")     // 来自 @athena/dsh
    const tree = ctx.get("researchTree")
    const researchSupervisor = ctx.get("researchSupervisor")
    // ... 组装 §5 的服务
    ctx.provide("autoResearchState", state)
    ctx.provide("hypothesisPool", pool)
    ctx.provide("autoResearchSupervisor", supervisor)
    // 注册模型可见工具（tools.ts）
    ctx.tools.register(autoresearchRunTool(ctx))
    ctx.tools.register(autoresearchStatusTool(ctx))
    ctx.tools.register(autoresearchPauseTool(ctx))
    ctx.tools.register(autoresearchResumeTool(ctx))
    ctx.tools.register(paperSetTemplateTool(ctx))
    ctx.tools.register(paperCompileTool(ctx))
    ctx.tools.register(paperDraftTool(ctx))
    // 生命周期
    ctx.effect(() => {
      supervisor.stop()  // disposer
    })
  }
}
```

## 7. 模型可见工具

| 工具 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `autoresearch_run` | 无 | `{run_id, phase, status}` | 启动/恢复全流程；若已 RUNNING 则幂等返回当前状态 |
| `autoresearch_status` | 无 | `{phase, status, budgets, pool_stats, sota, current_gate}` | 只读快照 |
| `autoresearch_pause` | 无 | `{status}` | 状态置 WAITING 并持久化 |
| `autoresearch_resume` | 无 | `{status}` | WAITING → RUNNING，按阶段续跑 |
| `paper_set_template` | `{template}` | `{paper_spec}` | 仅 INTAKE 阶段可调用；写 PaperSpec |
| `paper_compile` | 无 | `{ok, console_text, log_ref, pdf_ref?}` | 手动触发编译；控制台原文返回 |
| `paper_draft` | 无 | `{draft_ref}` | 返回当前 Markdown 草稿 artifact |

所有工具执行结果同时渲染为控制台文本；大对象只返回 ArtifactRef。

## 8. 阶段执行伪代码

### 8.1 INTAKE

```
entry:
  parse task/config
  spec = PaperSpec.from(config)
  templateKit.fetch(spec.template)   # 失败→ WAITING + 控制台错误
  state = StateStore.create({spec, budgets})
  publish phase_started
exit: IDEATION
```

### 8.2 IDEATION

```
while pool.queued().length < max_ideas and not tokens_exhausted:
  candidates = runIdeation(batch_size)         # 复用 Athena Ideator / idea_generation
  for c in candidates:
    if pool.findDuplicates(c.statement, 0.8).length > 0: continue
    if gate(c): pool.upsert(QUEUED)
    else: pool.upsert(REJECTED) + feedback
exit: EXPERIMENT（pool.queued() ≥ 1）
  or WAITING（无候选且预算耗尽，gate_result=IDEATION_EXHAUSTED）
```

### 8.3 EXPERIMENT（Athena 子集）

```
register pool.queued() 到 ResearchTree（等价 register_hypotheses）
await researchSupervisor.start()               # PREPARE → SEARCH → VALIDATE
wait until ResearchState.phase == COMPLETED or WAITING
on each settled experiment:
  pool.recordSettlement(hypothesis_id, outcome)
exit: WRITING（research_phase == COMPLETED）
```

默认 `experiment_strategy="athena_rolling"`，必须与当前 Athena 输出一致；`"mcts"`/`"pool_greedy"` 作为后续策略插槽。

### 8.4 WRITING → REFINEMENT → PACKAGING

```
evidence = pool.exportForPaper()
paperDir = composer.compose({tree, pool, evidence, spec})
# Markdown draft 始终生成
composer.writeMarkdownDraft(...)

# 内容质量修订循环（受 max_paper_rounds 限制）
for round in 1..max_paper_rounds:
  compileResult = compile(spec, paperDir)      # overleaf | local | none
  gate = reviewer.review({paperDir, evidence, spec, compileResult})
  if gate.pass: break
  publish gate_failed(blocking_factor, feedback)
  paperDir = composer.revise(paperDir, gate.feedback)

# 编译修复独立循环（不受 max_paper_rounds 限制，只受 project_time_limit）
while compileResult.ok == false and not stateStore.deadlineReached(state):
  publish paper_compile_output(consoleText, logRef)
  paperDir = composer.reviseLatex(paperDir, compileResult.log)   # 编译日志原文喂回
  compileResult = compile(spec, paperDir)

if compileResult.ok: packaging.package(...)
else: freeze sources + logs + compile_ok=false; phase=PACKAGING（带未过标记）或 WAITING
```

### 8.5 总控循环

```
AutoResearchSupervisor.run():
  while state.phase != COMPLETED and state.status != STOPPED:
    if stateStore.deadlineReached(state): state.status = WAITING; break
    stageRunner[state.phase](state)
    stateStore.save(state)
    publish state
```

## 9. 事件目录

| 事件名 | payload 要点 |
|---|---|
| `autoresearch/phase_started` | `{run_id, phase, round}` |
| `autoresearch/phase_completed` | `{run_id, phase, gate_result}` |
| `autoresearch/gate_failed` | `{run_id, phase, blocking_factor, feedback}` |
| `autoresearch/budget_exhausted` | `{run_id, budget, phase}` |
| `autoresearch/paper_version` | `{run_id, version, draft_ref, pdf_ref?, gate_result}` |
| `paper_compile_output` | `{run_id, ok, console_text, log_ref, compiler}` |
| `hypothesis_pool_changed` | `{run_id, pool_id, pool_status}` |
| `autoresearch/state` | 全量状态快照（与现有 `state` 事件同风格） |

事件中的自然语言文本统一过 `redact`；大对象只走 ArtifactRef。

## 10. 错误处理与恢复

| 故障 | 处理 |
|---|---|
| 模板抓取失败 | 回退缓存；无缓存 → INTAKE 置 `WAITING`，控制台返回 curl 错误 |
| Overleaf API 失败 | 降级 `local`；本机无 TeX 降级 `none`；状态记录 `paper_latex_via_degraded` |
| 本机 TeX 编译失败 | 进入编译修复循环（§8.4），不消耗 `max_paper_rounds` |
| 阶段内 LLM worker 失败 | 阶段内重试 1 次；仍失败置 `WAITING`，保留阶段状态 |
| 进程崩溃 | 启动时 `StateStore.load` + `HypothesisPool.reconcile(tree)`；EXPERIMENT 委托 `researchSupervisor.recover` |
| 时间耗尽 | 任意阶段停在当前状态，置 `WAITING`，持久化 deadline 与未完成项 |
| 池写失败 | 不阻塞实验结算；告警 + 下次 `reconcile` 修复（池是增强索引，树是事实源） |

## 11. 安全与隔离

- Overleaf token 只从环境变量读，不落 `state.json`/`hypothesis_pool.json`。
- 模板抓取使用 URL 白名单；禁止任意 URL。
- 论文源文件、编译日志可被 LLM 读取，但 evaluator、数据分片、baseline 产物保持只读。
- 编译子进程使用 bounded argv（复用 `ExecutionRuntime` 风格），禁止 shell 字符串。

## 12. 测试计划

| 层 | 用例 |
|---|---|
| schemas | `AutoResearchState` 迁移矩阵、`Budgets` 默认值、`PaperSpec` 枚举、`PooledHypothesis` 不变式 |
| pool | 状态机迁移、对账、查重、`exportForPaper`、崩溃恢复 |
| template-kit | curl 成功/失败回退、URL 白名单、解压与 sha256 |
| latex-builder | 编译成功/失败、stdout/stderr 原文落盘、事件输出 |
| overleaf | mock 官方 API：建项目、同步、编译、日志、失败降级 |
| reviewer | 六项质量闸的通过/拒绝与 `blocking_factor` |
| supervisor | 阶段推进、预算耗尽、时间耗尽、编译修复循环无轮次上限、暂停/恢复 |
| plugin | 在 `new Context()` 中注册 `@athena/dsh` 服务后，`ctx.get("autoResearchSupervisor")` 可用、工具可执行 |
| integration | fake subagent + mock Overleaf 跑完整 INTAKE→PACKAGING；阴性结果进入 limitations；默认 `athena_rolling` 与 Athena 对拍 |

## 13. 验收标准

1. `@athena/autoresearch` 独立包可构建、`vitest` 全绿。
2. `autoresearchPlugin` 注册后，`ctx.get` 可取到 `autoResearchState`、`hypothesisPool`、`autoResearchSupervisor`。
3. `.athena/autoresearch/state.json` 与 `hypothesis_pool.json` 崩溃可恢复。
4. 默认 Experiment 阶段输出与当前 Athena 逐字节一致。
5. 三条论文路径均可产出：Markdown draft 始终存在；`local` 与 `overleaf` 在环境满足时产出 PDF。
6. 编译修复循环只受 `project_time_limit` 限制，不设轮次上限；每次尝试返回控制台/编译器输出。
7. 阴性结果全部进入 limitations/negative results 或显式归档。
8. 所有质量闸拒绝必须给出 `blocking_factor` 与证据。

## 14. 回滚与兼容

- `@athena/dsh` 与 `@athena/research` 现有服务不改契约；`autoresearchPlugin` 只消费不修改。
- 若 AutoResearch 不可用，Athena 工作流仍可独立运行。
- 配置文件新增字段均有默认值；旧 state 无 `autoresearch` 目录时视为未启用。

## 15. 开放项（实现期）

1. ICLR/ICML 模板官方 URL 白名单与授权确认。
2. Overleaf 官方 API 的速率限制、重试与幂等键。
3. `project_time_limit` 的默认值（建议首版 12h，可按任务类型覆盖）。
4. Paper Writer/Reviewer 的 subagent 预设名称与 `outputSchema`。
