# AutoResearch API 泛化设计

日期：2026-08-15
状态：M4 延期参考（不作为 ML v1 实现输入）
上游：`2026-08-15-autoresearch-generalization-and-minimalism.md`

---

## 0. 2026-08-20 延期说明

本文件保留为第二领域接入后的 API 设计素材。M1–M3 不实现本文的 `RunSpec`、
`PipelineRunner`、`StageContext`、`ProviderRegistry`、`GateRunner`、自定义 EventBus
或统一错误类体系。

首版只通过 DSH 注册 `AutoResearchService`，并用现有 handoff 跑通 ML 纵切。
待第二个真实领域或实验引擎接入后，逐项验证本文接口是否仍有必要；未被实际差异
证明的抽象不进入代码。当前执行基线见
[`2026-08-20-autoresearch-ml-control-plane-design.md`](2026-08-20-autoresearch-ml-control-plane-design.md)。

## 1. 目标

定义 AutoResearch 的**对外 API 面**：所有能力通过统一接口访问，新增能力不改核心。API 分四层：

1. **配置层**：`RunSpec` JSON。
2. **核心层**：`PipelineRunner` / `StageContext` / `ProviderRegistry`。
3. **Provider 层**：六类 Provider 接口。
4. **工具层**：DSH 模型可见工具。

## 2. 命名约定

- schema 用 `zod`，类型用 `z.infer`。
- 接口名以 `Provider` 结尾的是可插拔实现；以 `Port` 结尾的是内部依赖端口。
- 所有方法返回 `Promise`；文件 IO 走 `ArtifactStore`；控制台输出走 `ConsolePort`。
- 大对象只传 `ArtifactRef`，不传内联 base64。

## 3. 配置层：RunSpec

```ts
export interface RunSpec {
  schema: "autoresearch-run-spec/v1"
  run_id?: string
  stages: StageSpec[]
  providers: {
    experiment_engine: ProviderRef
    literature?: ProviderRef
    figure_skill: ProviderRef
    template: ProviderRef
    compiler: ProviderRef
    reviewer?: ProviderRef
  }
  budgets: Budgets
  paper_spec: PaperSpec
  gates?: GateRef[]
  timeout: {
    stage_ms: number
    project: string
  }
}

export interface ProviderRef {
  id: string
  version?: string
  config?: Record<string, unknown>
}

export interface StageSpec {
  id: string
  provider: string
  config?: Record<string, unknown>
  depends_on?: string[]
  gates?: GateRef[]
  allow_skip?: boolean
  retry?: { max: number; backoff_ms: number }
}

export type GateRef = string | { id: string; config?: Record<string, unknown> }
```

## 4. 核心层

### 4.1 PipelineRunner

```ts
export class PipelineRunner {
  constructor(deps: {
    stages: Map<string, Stage>
    stateStore: AutoResearchStateStore
    gateRunner: GateRunner
    events: AutoResearchEventBus
    console: ConsolePort
  }) {}

  async run(spec: RunSpec, ctx: StageContext): Promise<RunResult>
  async resume(spec: RunSpec, ctx: StageContext): Promise<RunResult>
  async stop(): Promise<void>
}
```

### 4.2 Stage

```ts
export interface Stage {
  readonly id: string
  canEnter(ctx: StageContext): boolean
  run(ctx: StageContext): Promise<StageResult>
}

export interface StageResult {
  status: "COMPLETED" | "WAITING" | "FAILED" | "SKIPPED"
  nextStage?: string
  artifacts?: Record<string, ArtifactRef>
  gateResults?: GateResult[]
  consoleLines?: string[]
}
```

### 4.3 StageContext

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

### 4.4 Gate / GateRunner

```ts
export interface Gate {
  readonly id: string
  readonly retryable: boolean
  check(ctx: StageContext, stageResult: StageResult): Promise<GateResult>
}

export interface GateResult {
  pass: boolean
  blockingFactor?: string
  itemScores: Array<{ item: string; score: 0 | 1; evidence: string }>
  feedback: string[]
}

export class GateRunner {
  register(gate: Gate): void
  run(gateRefs: GateRef[], ctx: StageContext, stageResult: StageResult): Promise<GateResult[]>
}
```

## 5. Provider 层

### 5.1 基础 Provider

```ts
export interface Provider {
  readonly id: string
  readonly version: string
  readonly capabilities: string[]
}
```

### 5.2 FigureSkillProvider（SVG skill 测试期）

```ts
export interface FigureSkillProvider extends Provider {
  readonly id: "native-svg" | "fireworks-tech-graph" | "drawio-skill" | "ink-graph"
  selfTest(ctx: StageContext): Promise<{ ok: boolean; issues: string[] }>
  generate(ctx: StageContext, input: FigureDesignInput): Promise<FigureDesignResult>
}

export interface FigureDesignInput {
  runId: string
  name: string                       // architecture | method | flow | ablation_overview
  kind: "architecture" | "method" | "flow" | "ablation_overview"
  context: {
    sotaPath: string[]
    evidence: PaperEvidenceBundle
    draftSectionRefs: ArtifactRef[]
  }
  constraints: {
    format: "svg"
    maxWidthPx: number
    maxHeightPx: number
    palette: string[]
    noExternalAssets: true
  }
}

export interface FigureDesignResult {
  svgRef: ArtifactRef
  svgPath: string
  renderedPdfRef?: ArtifactRef
  renderedPngRef?: ArtifactRef
  selfCheck: {
    syntaxOk: boolean
    viewboxOk: boolean
    textLegible: boolean
    paletteOk: boolean
    issues: string[]
  }
}
```

### 5.3 ExperimentEngineProvider

```ts
export interface ExperimentEngineProvider extends Provider {
  readonly id: "athena" | "mcts" | "external"
  start(ctx: StageContext): Promise<void>
  recover(ctx: StageContext): Promise<void>
  settleEvents(ctx: StageContext): AsyncIterable<SettlementEvent>
}

export interface SettlementEvent {
  hypothesisId: string
  experimentId: string
  outcome: "WIN" | "DRAW" | "LOSS" | "INCONCLUSIVE"
  metric: number | null
  referenceMetric: number | null
  direction: "maximize" | "minimize"
  sotaAfter: string | null
}
```

### 5.4 TemplateProvider / CompilerProvider / LiteratureProvider

```ts
export interface TemplateProvider extends Provider {
  readonly id: "curl" | "cache" | "bundled"
  fetch(venue: string): Promise<TemplateDir>
  list(): string[]
}

export interface CompilerProvider extends Provider {
  readonly id: "local" | "overleaf" | "none"
  compile(ctx: StageContext, paperDir: string): Promise<CompileResult>
}

export interface LiteratureProvider extends Provider {
  readonly id: "none" | "paper-rag"
  search(ctx: StageContext, query: string): Promise<LiteratureRecord[]>
  fetchEvidence(ctx: StageContext, refs: string[]): Promise<ArtifactRef[]>
}
```

### 5.5 ReviewerProvider

```ts
export interface ReviewerProvider extends Provider {
  readonly id: "builtin" | string
  review(ctx: StageContext, input: ReviewInput): Promise<GateResult[]>
}
```

## 6. 内部端口（Ports）

```ts
export interface IdeationPort {
  runBatch(ctx: StageContext, input: IdeationInput): Promise<IdeationBatchResult>
}

export interface PaperComposerPort {
  compose(ctx: StageContext, input: PaperInput): Promise<PaperDir>
  revise(ctx: StageContext, input: ReviseInput): Promise<PaperDir>
  reviseLatex(ctx: StageContext, input: ReviseLatexInput): Promise<PaperDir>
}

export interface PackagingPort {
  package(ctx: StageContext, input: PackagingInput): Promise<PackagingResult>
}
```

## 7. 事件 API

```ts
export interface AutoResearchEventBus {
  emit(event: AutoResearchEvent): Promise<void>
  subscribe(pattern: string, handler: (event: AutoResearchEvent) => void): () => void
}

export type AutoResearchEvent =
  | { type: "stage_started"; run_id: string; stage_id: string }
  | { type: "stage_completed"; run_id: string; stage_id: string; gate_results: GateResult[] }
  | { type: "stage_failed"; run_id: string; stage_id: string; blocking_factor: string; feedback: string[] }
  | { type: "provider_degraded"; run_id: string; capability: string; from: string; to: string; reason: string }
  | { type: "paper_compile_output"; run_id: string; ok: boolean; console_text: string; log_ref: ArtifactRef; compiler: string }
  | { type: "paper_version"; run_id: string; version: number; draft_ref: ArtifactRef; pdf_ref?: ArtifactRef; gate_result?: GateResult }
  | { type: "hypothesis_pool_changed"; run_id: string; pool_id: string; pool_status: string }
  | { type: "state"; run_id: string; state: AutoResearchState }
```

## 8. 错误 API

```ts
export class AutoResearchError extends Error {
  constructor(
    public readonly code:
      | "PROVIDER_DEGRADED"
      | "STAGE_GATE_FAILED"
      | "STAGE_TIMEOUT"
      | "BUDGET_EXHAUSTED"
      | "TIME_LIMIT_REACHED"
      | "SPEC_INVALID",
    message: string,
    public readonly detail?: unknown
  ) { super(message) }
}
```

规则：

- Provider 内部错误必须包装为 `AutoResearchError` 或带 `detail` 的 `PROVIDER_DEGRADED`。
- 核心只分支 6 类错误；`detail` 原样进日志/事件。

## 9. 工具层 API（DSH 可见）

```ts
export interface AutoResearchToolInputs {
  autoresearch_run: {}
  autoresearch_status: {}
  autoresearch_pause: {}
  autoresearch_resume: {}
  paper_set_template: { template: string }
  paper_compile: {}
  paper_draft: {}
}
```

工具返回统一信封：

```ts
export interface ToolEnvelope<T> {
  ok: boolean
  data?: T
  error?: { code: string; message: string }
  consoleLines: string[]
}
```

## 10. 版本与兼容

- RunSpec schema 版本 `"autoresearch-run-spec/v1"`；后续变更必须升版本。
- Provider 通过 `version` 标识；Provider 升级必须保持同 capability 的输入输出兼容。
- 核心 API 变更只允许加可选字段；删除/改名需升主版本。

## 11. 验收清单

- [ ] RunSpec v1 可被 zod 严格校验，非法字段报 `SPEC_INVALID`。
- [ ] 不修改核心，可注册新 Stage / Gate / Provider。
- [ ] 所有 Provider 输入输出只走 ArtifactRef / 原始类型，不传类实例。
- [ ] 所有控制台信息经 ConsolePort，所有大对象经 ArtifactStore。
- [ ] 默认 preset 不依赖第三方 SVG skill，且可切换第三方 provider。
