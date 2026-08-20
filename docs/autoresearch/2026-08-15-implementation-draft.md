# AutoResearch 实现草稿（implementation draft）

日期：2026-08-15
状态：已被 2026-08-20 ML-first 精简设计取代，仅作历史参考
目标：把设计文档落到 `athena_ts/packages/athena-autoresearch` 的具体实现草稿。

---

## 0. 不要直接实施本草稿

本文件中的 `PipelineRunner`、Provider/Gate/EventBus 层次和大批 schema 是早期探索，
不属于 ML v1。当前实现必须从
[`2026-08-20-autoresearch-ml-control-plane-design.md`](2026-08-20-autoresearch-ml-control-plane-design.md)
出发，复用 DSH 与既有 handoff，仅补一个主要 `AutoResearchService`、精简状态、
动态 rubric、自主循环和 Athena 薄适配。

本文件可用于查找早期算法草图，但不得据此扩张首版代码范围。

## 1. 包与依赖

```jsonc
// athena_ts/packages/athena-autoresearch/package.json
{
  "name": "@athena/autoresearch",
  "private": true,
  "type": "module",
  "dependencies": {
    "@athena/core": "workspace:*",
    "@athena/research": "workspace:*",
    "@athena/dsh": "workspace:*",
    "zod": "^4.0.0"
  }
}
```

不直接依赖 `@deepseek-ai/*`；DSH 环境类型通过 `@athena/dsh` 再导出。

## 2. 目录草案

```
packages/athena-autoresearch/
  src/
    index.ts
    schemas/
      run-spec.ts
      state.ts
      paper-spec.ts
      hypothesis-pool.ts
      events.ts
      errors.ts
    core/
      pipeline-runner.ts
      stage-context.ts
      provider-registry.ts
      gate-runner.ts
      budget-service.ts
      console-port.ts
    providers/
      experiment/
        athena-engine.ts
        types.ts
      figure-skill/
        types.ts
        native-svg.ts
        fireworks-tech-graph.ts
        drawio-skill.ts
        ink-graph.ts
      template/
        types.ts
        curl-template.ts
        bundled-template.ts
      compiler/
        types.ts
        local-tex.ts
        overleaf.ts
        none-compiler.ts
      literature/
        types.ts
        none-literature.ts
      reviewer/
        builtin-reviewer.ts
    stages/
      intake.ts
      ideation.ts
      experiment.ts
      writing.ts
      refinement.ts
      packaging.ts
    plugin.ts
    tools.ts
  test/
    ...
```

## 3. 核心代码草图

### 3.1 RunSpec schema

```ts
import { z } from "zod"

export const ProviderRefSchema = z.object({
  id: z.string(),
  version: z.string().optional(),
  config: z.record(z.string(), z.unknown()).optional(),
})

export const StageSpecSchema = z.object({
  id: z.string(),
  provider: z.string(),
  config: z.record(z.string(), z.unknown()).optional(),
  depends_on: z.array(z.string()).optional(),
  gates: z.array(z.string()).optional(),
  allow_skip: z.boolean().optional(),
  retry: z.object({ max: z.number().int().min(0), backoff_ms: z.number().int().min(0) }).optional(),
})

export const RunSpecSchema = z.object({
  schema: z.literal("autoresearch-run-spec/v1"),
  run_id: z.string().optional(),
  stages: z.array(StageSpecSchema).min(1),
  providers: z.object({
    experiment_engine: ProviderRefSchema,
    literature: ProviderRefSchema.optional(),
    figure_skill: ProviderRefSchema,
    template: ProviderRefSchema,
    compiler: ProviderRefSchema,
    reviewer: ProviderRefSchema.optional(),
  }),
  budgets: z.object({
    max_ideas: z.number().int().min(1),
    max_experiments: z.number().int().min(1),
    max_paper_rounds: z.number().int().min(1),
    max_tokens: z.number().int().min(0),
    project_time_limit: z.string(),
  }),
  paper_spec: z.record(z.string(), z.unknown()),
  timeout: z.object({
    stage_ms: z.number().int().min(1000),
    project: z.string(),
  }),
})
```

### 3.2 StageContext

```ts
export interface StageContext {
  readonly runId: string
  readonly spec: RunSpec
  readonly state: AutoResearchState
  readonly pool: HypothesisPool
  readonly tree: ResearchTree
  readonly artifacts: ArtifactStore
  readonly providers: ProviderRegistry
  readonly budgets: BudgetService
  readonly events: AutoResearchEventBus
  readonly console: ConsolePort
}
```

### 3.3 Stage 接口

```ts
export interface Stage {
  readonly id: string
  canEnter(ctx: StageContext): boolean
  run(ctx: StageContext): Promise<StageResult>
}

export type StageStatus = "COMPLETED" | "WAITING" | "FAILED" | "SKIPPED"

export interface StageResult {
  status: StageStatus
  nextStage?: string
  artifacts?: Record<string, ArtifactRef>
  gateResults?: GateResult[]
  consoleLines?: string[]
}
```

### 3.4 PipelineRunner

```ts
export class PipelineRunner {
  constructor(opts: {
    stages: Map<string, Stage>
    stateStore: AutoResearchStateStore
    gateRunner: GateRunner
  }) {}

  async run(ctx: StageContext): Promise<void> {
    const spec = ctx.spec
    const stageMap = new Map(spec.stages.map(s => [s.id, s]))
    const done = new Set<string>()

    while (ctx.state.status === "RUNNING") {
      if (ctx.budgets.timeLimitReached()) {
        ctx.state.status = "WAITING"
        ctx.state.stoppedBy = "TIME_LIMIT_REACHED"
        break
      }
      const next = spec.stages.find(s =>
        !done.has(s.id) &&
        (s.depends_on ?? []).every(d => done.has(d)) &&
        (this.stages.get(s.provider)?.canEnter(ctx) ?? false)
      )
      if (!next) {
        ctx.state.status = "WAITING"
        ctx.state.stoppedBy = "NO_ENTERABLE_STAGE"
        break
      }
      const stage = this.stages.get(next.provider)!
      const result = await this.withRetry(next, () => stage.run(ctx))
      done.add(next.id)
      ctx.state.phase = next.id
      ctx.state.stageId = next.id
      if (result.status === "FAILED") {
        ctx.state.status = "FAILED"
        break
      }
      if (result.status === "WAITING") {
        ctx.state.status = "WAITING"
        break
      }
      const gates = [...(spec.gates ?? []), ...(next.gates ?? [])]
      const gateResults = await this.gateRunner.run(gates, ctx, result)
      ctx.state.gateResults = gateResults
      if (!gateResults.every(g => g.pass)) {
        ctx.state.status = "WAITING"
        ctx.state.blockingFactor = gateResults.find(g => !g.pass)!.blockingFactor
        break
      }
    }
  }
}
```

### 3.5 ProviderRegistry

```ts
export interface Provider {
  readonly id: string
  readonly version: string
  readonly capabilities: string[]
}

export class ProviderRegistry {
  private providers = new Map<string, Provider>()

  register<T extends Provider>(p: T): T {
    if (this.providers.has(p.id)) throw new Error(`duplicate provider: ${p.id}`)
    this.providers.set(p.id, p)
    return p
  }

  get<T extends Provider>(id: string): T {
    const p = this.providers.get(id)
    if (!p) throw new Error(`unknown provider: ${id}`)
    return p as T
  }

  list(capability?: string): Provider[] {
    return [...this.providers.values()].filter(
      p => !capability || p.capabilities.includes(capability)
    )
  }
}
```

### 3.6 BudgetService

```ts
export class BudgetService {
  constructor(private budgets: Budgets, private startedAt: Date) {}

  addTokens(n: number) {
    this.budgets.tokens_used += n
  }

  canSpendTokens(n: number): boolean {
    return this.budgets.max_tokens === 0 || this.budgets.tokens_used + n <= this.budgets.max_tokens
  }

  ideasRemaining(pool: HypothesisPool): number {
    return this.budgets.max_ideas - pool.countByOrigin("ideation")
  }

  timeLimitReached(): boolean {
    return Date.now() >= this.startedAt.getTime() + parseDuration(this.budgets.project_time_limit)
  }
}
```

### 3.7 ConsolePort

```ts
export interface ConsolePort {
  info(msg: string): void
  error(msg: string): void
  compileOutput(output: {
    ok: boolean
    consoleText: string
    logRef: ArtifactRef
    compiler: string
  }): void
}
```

## 4. Provider 实现草稿

### 4.1 FigureSkillProvider

```ts
export interface FigureSkillProvider extends Provider {
  readonly id: "native-svg" | "fireworks-tech-graph" | "drawio-skill" | "ink-graph"
  selfTest(ctx: StageContext): Promise<{ ok: boolean; issues: string[] }>
  generate(ctx: StageContext, input: FigureDesignInput): Promise<FigureDesignResult>
}
```

`NativeSvgProvider` 草稿：

```ts
export class NativeSvgProvider implements FigureSkillProvider {
  readonly id = "native-svg" as const
  readonly version = "0.1.0"
  readonly capabilities = ["figure.svg"]

  async selfTest() {
    return { ok: true, issues: [] }
  }

  async generate(ctx: StageContext, input: FigureDesignInput): Promise<FigureDesignResult> {
    const svg = this.renderTemplate(input)   // 确定性模板：标题 + 模块框 + 箭头
    const svgRef = await ctx.artifacts.putText(svg)
    return {
      svgRef,
      svgPath: "figures/" + input.name + ".svg",
      selfCheck: { syntaxOk: true, viewboxOk: true, textLegible: true, paletteOk: true, issues: [] },
    }
  }
}
```

第三方 skill provider 草稿（测试期）：

```ts
export class FireworksTechGraphProvider implements FigureSkillProvider {
  readonly id = "fireworks-tech-graph" as const
  readonly version = "test"
  readonly capabilities = ["figure.svg"]

  async selfTest(ctx: StageContext) {
    try {
      const sample = await ctx.providers.get<FigureSkillProvider>("fireworks-tech-graph").generate(ctx, SAMPLE_INPUT)
      return { ok: sample.selfCheck.syntaxOk, issues: sample.selfCheck.issues }
    } catch (e) {
      return { ok: false, issues: [String(e)] }
    }
  }
}
```

### 4.2 ExperimentEngineProvider

```ts
export interface ExperimentEngineProvider extends Provider {
  readonly id: "athena" | "mcts" | "external"
  start(ctx: StageContext): Promise<void>
  recover(ctx: StageContext): Promise<void>
  settleEvents(ctx: StageContext): AsyncIterable<SettlementEvent>
}
```

`AthenaExperimentEngine` 草稿：

```ts
export class AthenaExperimentEngine implements ExperimentEngineProvider {
  readonly id = "athena" as const
  readonly version = "0.1.0"
  readonly capabilities = ["experiment.engine"]

  async start(ctx: StageContext) {
    const supervisor = ctx.providers.get<FixedFlowSupervisor>("@athena/dsh/researchSupervisor")
    // 把 pool.queued() 注册进树
    const candidates = ctx.pool.queued().map(p => p.hypothesis)
    if (candidates.length > 0) {
      await supervisor.registerHypotheses(candidates)
    }
    await supervisor.start()
  }

  async recover(ctx: StageContext) {
    const supervisor = ctx.providers.get<FixedFlowSupervisor>("@athena/dsh/researchSupervisor")
    await supervisor.recover()
    ctx.pool.reconcile(ctx.tree)
  }

  async *settleEvents(ctx: StageContext): AsyncIterable<SettlementEvent> {
    // 首版：从 ResearchTree 与 pool 的状态差量推导；后续替换为 @athena/research 的 settlement 回调
    yield* deriveSettlements(ctx.tree, ctx.pool)
  }
}
```

### 4.3 CompilerProvider

```ts
export interface CompilerProvider extends Provider {
  readonly id: "local" | "overleaf" | "none"
  compile(ctx: StageContext, paperDir: string): Promise<CompileResult>
}

export interface CompileResult {
  ok: boolean
  pdfRef?: ArtifactRef
  logRef: ArtifactRef
  consoleText: string
  compiler: string
}
```

## 5. Stage 实现草稿

```ts
export class IntakeStage implements Stage {
  readonly id = "intake"
  canEnter() { return true }
  async run(ctx: StageContext) {
    const template = ctx.providers.get<TemplateProvider>(ctx.spec.providers.template.id)
    const dir = await template.fetch(ctx.state.paperSpec.template)
    ctx.state.paperSpec.templateDir = dir.root
    return { status: "COMPLETED" }
  }
}

export class IdeationStage implements Stage {
  readonly id = "ideation"
  canEnter(ctx: StageContext) {
    return ctx.budgets.ideasRemaining(ctx.pool) > 0
  }
  async run(ctx: StageContext) {
    const target = Math.min(ctx.budgets.ideasRemaining(ctx.pool), DEFAULT_BATCH)
    const batch = await ctx.providers.get<IdeationPort>("ideation").runBatch({
      target,
      budget: { maxIdeas: ctx.budgets.ideasRemaining(ctx.pool), maxTokens: ctx.budgets.max_tokens },
    })
    for (const c of batch.candidates) ctx.pool.upsert(toPooled(c))
    return { status: "COMPLETED" }
  }
}
```

其余 stage 类似：`ExperimentStage` 调 `ExperimentEngineProvider`；`WritingStage` 调 `PaperComposer` 与 `FigureSkillProvider`；`RefinementStage` 调 `PaperReviewer` 与 `CompilerProvider`；`PackagingStage` 调 `PackagingService`。

## 6. 实现里程碑

| 里程碑 | 内容 | 完成标志 |
|---|---|---|
| M-0 | schemas + StateStore + Pool + ProviderRegistry | 纯单测绿 |
| M-1 | PipelineRunner + BudgetService + GateRunner | 线性五阶段 + 依赖 DAG 单测绿 |
| M-2 | AthenaExperimentEngine + 默认 preset | 与 `@athena/dsh` 集成测试绿 |
| M-3 | NativeSvgProvider + EvidencePlotter + AblationCompiler | figure 自检与数据图单测绿 |
| M-4 | LocalTex + Overleaf（mock）+ Markdown 草稿 | 三路径编译测试绿 |
| M-5 | 第三方 SVG skill provider 适配层（测试期） | selfTest 与降级测试绿 |
| M-6 | plugin + tools + BDD 场景 | 端到端 fake subagent 全流程绿 |

## 7. 测试草稿

- `test/schemas/run-spec.test.ts`：schema 校验、非法字段、深度合并。
- `test/core/pipeline-runner.test.ts`：线性、DAG、skip、retry、超时。
- `test/core/provider-registry.test.ts`：注册、查找、能力过滤。
- `test/providers/figure-skill/native-svg.test.ts`：自检与生成。
- `test/providers/experiment/athena-engine.test.ts`：默认行为与 Athena 对拍。
- `test/stages/*.test.ts`：每个 stage 的进入条件与结果。
- `test/plugin.test.ts`：在 `new Context()` 中注册后服务可用、工具可执行。

## 8. 备注

- 本文是草稿，类型与函数签名会在实现时微调。
- 不修改 `@athena/dsh` / `@athena/research` 现有公开契约；若需要 settlement 回调，先在 `@athena/research` 增加空默认回调。
