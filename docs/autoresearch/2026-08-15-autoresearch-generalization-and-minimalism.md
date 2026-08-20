# AutoResearch 泛化与极简架构设计（RunSpec + Provider 可插拔）

日期：2026-08-15
状态：历史泛化方案（M4 参考，不是 ML v1 实现基线）
原始原则：**泛化 / 灵活 > 极简**。该原则延后到第二个真实领域接入时应用。
上游：`2026-08-15-autoresearch-ts-plugin-design.md`、`2026-08-15-autoresearch-detailed-design.md`

---

## 0. 2026-08-20 范围收敛

ML v1 改为“领域语义完整、代码结构精简”。当前执行基线见
[`2026-08-20-autoresearch-ml-control-plane-design.md`](2026-08-20-autoresearch-ml-control-plane-design.md)。

- `RunSpec / PipelineRunner / StageContext / ProviderRegistry / GateRunner / EventBus`
  不在 M1–M3 实现；
- DSH 承担运行时与插件基础设施，现有 handoff 承担流程和领域知识；
- 首版只做 `AutoResearchService + ml.default + AthenaExperimentProvider` 的真实纵切；
- 本文的可替换性原则和 Provider 分类保留为 M4 候选，不是预先创建 interface 的清单；
- 只有第二个真实领域或实验引擎接入时，才从实际差异中提炼最小通用契约。

本文后续章节用于解释长期方向；与上述范围冲突的实现要求均延期到 M4。

## 1. 本文目的

前序设计定义了默认五阶段与一批具体服务（FigureDesigner / OverleafConnector / LatexBuilder…）。本文把架构进一步抽象：

- AutoResearch 核心只保留 **RunSpec → PipelineRunner → StageContext → Provider** 四个概念。
- 五阶段、Athena 子集、SVG skill、Overleaf/本机 TeX 都退化为**默认 preset 与可替换 Provider**。
- SVG skill 仍在测试效果，因此**不在架构层绑定任何具体 skill**，只定义 `FigureSkillProvider` 接口与测试用适配器。

## 2. 原则落地

| 原则 | 架构表现 |
|---|---|
| **泛化优先** | 阶段、实验引擎、图生成、论文模板、编译器、文献源、门禁全部 Provider 化；RunSpec 配置驱动，默认 preset 只是配置文件 |
| **极简** | 核心只保留 PipelineRunner / Stage / StageContext / Provider / Gate / EventBus，不再为核心增加更多服务 |
| **灵活 > 极简** | 允许 Provider 数量多于 1、允许 Stage 条件与依赖、允许默认 preset 之外的 stage 顺序；这些“额外复杂度”被显式接受 |

## 3. 核心抽象

```
RunSpec (JSON / zod)
   │  描述一次 AutoResearch 运行：阶段序列、provider、budget、gates
   ▼
PipelineRunner (唯一执行器)
   │  按 RunSpec 依次执行 Stage；负责状态机、预算、事件、恢复
   ▼
StageContext (运行上下文)
   │  给每个 Stage 提供依赖：state, pool, tree, eventBus, providers, budget
   ▼
Provider (可插拔实现)
   │  FigureSkillProvider / ExperimentEngineProvider / TemplateProvider /
   │  CompilerProvider / LiteratureProvider / ReviewerProvider / ...
   ▼
Gate (可插拔质量闸)
   │  每个 Stage 可挂多个 Gate；Gate 不通过 → 修订 / 等待 / 失败
```

### 3.1 `RunSpec`

```ts
interface RunSpec {
  schema: "autoresearch-run-spec/v1"
  run_id?: string
  stages: StageSpec[]                 // 显式顺序，允许任意编排
  providers: {
    experiment_engine: ProviderRef    // 默认 "athena"
    literature?: ProviderRef          // 默认 "none"
    figure_skill: ProviderRef         // 默认 "native-svg"
    template: ProviderRef             // 默认 "plain_latex"
    compiler: ProviderRef             // 默认 "local"；可 "overleaf" / "none"
    reviewer?: ProviderRef            // 默认 "builtin"
  }
  budgets: Budgets
  paper_spec: PaperSpec
  gates: GateRef[]                    // 默认内置六项
  timeout: { stage_ms: number; project: string }
}
```

### 3.2 `Stage`

```ts
interface StageSpec {
  id: string                          // 全局唯一
  provider: string                    // stage 实现，如 "ideation" / "experiment" / "writing"
  config?: Record<string, unknown>    // 传给 provider
  gates?: GateRef[]                   // 阶段后置质量闸
  depends_on?: string[]               // 阶段依赖（默认线性）
  allow_skip?: boolean
  retry?: { max: number; backoff_ms: number }
}

interface Stage {
  readonly id: string
  canEnter(ctx: StageContext): boolean
  run(ctx: StageContext): Promise<StageResult>
}

interface StageResult {
  status: "COMPLETED" | "WAITING" | "FAILED" | "SKIPPED"
  next_phase?: string
  artifacts?: Record<string, ArtifactRef>
  gate_results?: GateResult[]
}
```

### 3.3 `StageContext`

```ts
interface StageContext {
  run_id: string
  state: AutoResearchState
  pool: HypothesisPool
  tree: ResearchTree
  artifacts: ArtifactStore
  eventBus: AutoResearchEventBus
  providers: ProviderRegistry
  budgets: Budgets
  logger: ConsoleLogger             // 控制台输出统一入口
}
```

### 3.4 `Provider`

```ts
interface Provider {
  readonly id: string
  readonly version: string
  readonly capabilities: string[]
}

interface ProviderRegistry {
  register(p: Provider): void
  get<T extends Provider>(id: string): T
  list(capability?: string): Provider[]
}
```

所有外部能力（SVG skill、Overleaf、Athena、模板抓取）都实现为 Provider。新增能力 = 新增 Provider，不改 PipelineRunner。

## 4. 最小核心 vs 扩展

| 层 | 模块 | 泛化/极简取舍 |
|---|---|---|
| **核心（不轻易变）** | `RunSpec` schema、`PipelineRunner`、`StageContext`、`ProviderRegistry`、`AutoResearchState`、`HypothesisPool`、`EventBus` | 只做“运行一个可插拔 stage 图” |
| **默认 preset** | `presets/default.json`（五阶段 + Athena + builtin gates） | 当前 Athena 工作流成为 preset，不再是硬编码 |
| **Providers（可增删）** | `athena-engine`、`mcts-engine`、`native-svg`、`fireworks-svg`、`drawio-svg`、`overleaf`、`local-tex`、`markdown`、`builtin-reviewer` | 每个 provider 独立测试，坏掉可替换 |
| **Gates（可增删）** | `compile_ok`、`evidence_traceable`、`template_compliant`、`negative_results_complete`、`claim_consistency`、`novelty_statement`、`figure_source_valid`、`eda_figures_absent`、`ablation_complete`、`experiment_design_ok` | Gate 是可插拔 list，不写死在 Reviewer 内 |

## 5. Stage 可插拔设计

### 5.1 Stage 注册表

```ts
const STAGE_REGISTRY: Record<string, Stage> = {
  intake: IntakeStage,
  ideation: IdeationStage,
  experiment: ExperimentStage,
  writing: WritingStage,
  refinement: RefinementStage,
  packaging: PackagingStage,
}
```

默认 preset 的 `stages` 为：

```json
[
  { "id": "intake", "provider": "intake" },
  { "id": "ideation", "provider": "ideation", "depends_on": ["intake"] },
  { "id": "experiment", "provider": "experiment", "depends_on": ["ideation"] },
  { "id": "writing", "provider": "writing", "depends_on": ["experiment"] },
  { "id": "refinement", "provider": "refinement", "depends_on": ["writing"] },
  { "id": "packaging", "provider": "packaging", "depends_on": ["refinement"] }
]
```

### 5.2 允许的泛化

- 可插入新 stage（如 `survey`、`rebuttal`、`camera_ready`）而不改核心。
- 可跳过 stage（`allow_skip: true`）。
- 可并行 stage（`depends_on` 不限制同层并行；`PipelineRunner` 以依赖 DAG 调度）。
- 每个 stage 可有自己的 gates 与 retry；默认全局 gates 与 stage gates 取并集。

### 5.3 PipelineRunner 算法

```
load state or create from RunSpec
while state.phase != COMPLETED and status == RUNNING:
  next = stages.find(s => canEnter(s) and s.depends_on satisfied)
  if none: status = WAITING; break
  result = await next.run(ctx)
  apply result to state (phase/status/artifacts)
  for gate in next.gates:
    if gate(state, ctx) fail:
      if gate.retryable and budget remains: revise/rerun
      else: status = WAITING or FAILED
  save state
```

## 6. Provider 可插拔设计

### 6.1 FigureSkillProvider（SVG skill 测试期）

```ts
interface FigureSkillProvider extends Provider {
  readonly id: string
  generate(input: FigureDesignInput): Promise<FigureDesignResult>
  selfTest(): Promise<{ ok: boolean; sample_svg_ref?: ArtifactRef; issues: string[] }>
}

const FIGURE_SKILL_PROVIDERS = {
  "native-svg": NativeSvgProvider,          // 极简内置：规则化 SVG 模板
  "fireworks-tech-graph": FireworksTechGraphProvider,  // 测试中
  "drawio-skill": DrawioSkillProvider,                  // 测试中
  "ink-graph": InkGraphProvider,                         // 测试中
}
```

选择规则：

- `paper_spec.figure_skill` 决定使用哪个 provider；缺省 `"native-svg"`。
- 每次 WRITING 开始前，`FigureSkillProvider.selfTest()` 跑一次：
  - 通过：使用该 provider。
  - 失败：自动降级 `"native-svg"`，发布 `figure_skill_degraded` 事件。
- 由于 SVG skill 仍在测试效果，**默认 preset 不绑定任何第三方 skill**；第三方 skill 只作为可选 provider 挂载。

`NativeSvgProvider`（极简内置兜底）：

- 用确定性模板生成“标题 + 模块框 + 箭头”的架构图，保证语法通过、可编辑文本、无外部资源。
- 视觉效果弱，但流程永远可跑通；测试期结束后可把它降级为 fallback。

### 6.2 ExperimentEngineProvider

```ts
interface ExperimentEngineProvider extends Provider {
  start(ctx: StageContext): Promise<void>
  recover(ctx: StageContext): Promise<void>
  settleEvents(ctx: StageContext): AsyncIterable<SettlementEvent>
}

const EXPERIMENT_ENGINE_PROVIDERS = {
  "athena": AthenaExperimentEngine,     // 当前 Athena 子集
  "mcts": MctsExperimentEngine,         // 未来
  "external": ExternalExperimentEngine, // 未来：外部实验平台
}
```

### 6.3 TemplateProvider / CompilerProvider / LiteratureProvider

```ts
interface TemplateProvider extends Provider {
  fetch(venue: string): Promise<TemplateDir>
  list(): TemplateVenue[]
}

interface CompilerProvider extends Provider {
  compile(paperDir: string): Promise<CompileResult>
}

interface LiteratureProvider extends Provider {
  search(query: string): Promise<LiteratureRecord[]>
  fetchEvidence(refs: string[]): Promise<ArtifactRef[]>
}
```

- `TemplateProvider` 默认 `"curl"`（每次启动抓官网）；可替换 `"cache"` 或 `"bundled"`。
- `CompilerProvider` 默认 `"local"`；可选 `"overleaf"` / `"none"`。
- `LiteratureProvider` 默认 `"none"`；接入 `paper_rag` 后为 `"paper-rag"`。

## 7. 配置驱动：默认 preset

`presets/autoresearch.default.json`：

```jsonc
{
  "schema": "autoresearch-run-spec/v1",
  "stages": [
    { "id": "intake", "provider": "intake" },
    { "id": "ideation", "provider": "ideation" },
    { "id": "experiment", "provider": "experiment" },
    { "id": "writing", "provider": "writing" },
    { "id": "refinement", "provider": "refinement" },
    { "id": "packaging", "provider": "packaging" }
  ],
  "providers": {
    "experiment_engine": "athena",
    "literature": "none",
    "figure_skill": "native-svg",
    "template": "curl",
    "compiler": "local",
    "reviewer": "builtin"
  },
  "budgets": {
    "max_ideas": 20,
    "max_experiments": 10,
    "max_paper_rounds": 5,
    "max_tokens": 0,
    "project_time_limit": "PT12H"
  },
  "paper_spec": {
    "template": "plain_latex",
    "main_language": "latex",
    "latex_via": "local",
    "local_compiler": "latexmk"
  }
}
```

用户可提供 `--spec path` 或 `autoresearch_run` 工具参数覆盖默认 preset；覆盖是**深度合并**，未覆盖字段保持 preset 默认。

## 8. 状态机泛化

- `AutoResearchState` 不再硬编码五阶段枚举，改为 `phase: string` + `stage_id: string`。
- 合法迁移由 RunSpec 的 stage 列表动态生成，而不是写死枚举。
- `status` 仍为 `RUNNING / WAITING / STOPPED / FAILED / COMPLETED`（核心状态不变）。

```jsonc
{
  "phase": "refinement",          // stage_id，由 RunSpec 决定
  "stage_id": "refinement",
  "status": "RUNNING"
}
```

## 9. 事件泛化

| 事件 | 说明 |
|---|---|
| `autoresearch/stage_started` | `{run_id, stage_id}` |
| `autoresearch/stage_completed` | `{run_id, stage_id, gate_results}` |
| `autoresearch/stage_failed` | `{run_id, stage_id, blocking_factor, feedback}` |
| `autoresearch/provider_degraded` | `{run_id, capability, from, to, reason}` |
| `autoresearch/paper_compile_output` | `{run_id, ok, console_text, log_ref, compiler}` |
| `autoresearch/state` | 全量状态快照 |

泛化点：`stage_started` 替代原来每个阶段一个事件类型；前端按 `stage_id` 渲染，不需要知道所有阶段名。

## 10. 错误处理泛化

| 错误码 | 泛化语义 |
|---|---|
| `PROVIDER_DEGRADED` | 某 Provider 失败并降级 |
| `STAGE_GATE_FAILED` | 某阶段后置闸未过 |
| `STAGE_TIMEOUT` | 阶段超时 |
| `BUDGET_EXHAUSTED` | 预算耗尽 |
| `TIME_LIMIT_REACHED` | 项目时间限制 |
| `SPEC_INVALID` | RunSpec 非法 |

原则：核心只认识上述 6 类错误；具体 Provider 的细节错误包装进 `detail` 字段，不进入核心分支。

## 11. 验收标准（增量）

1. 用 `presets/autoresearch.default.json` 跑出的行为与上一版详细设计的五阶段一致。
2. 不修改核心，可加入一个新 stage（如 `camera_ready`）并在 RunSpec 中启用。
3. 不修改核心，可新增一个 `FigureSkillProvider`（如 `fireworks-svg`）并在 `paper_spec.figure_skill` 中切换。
4. `FigureSkillProvider.selfTest()` 失败自动降级 `native-svg`。
5. 默认 preset 不依赖任何第三方 SVG skill；第三方 skill 只作可选 provider。
6. `PipelineRunner` 可执行带 `depends_on` 的 stage DAG，且支持 `allow_skip`。
7. RunSpec 非法时抛出 `SPEC_INVALID`，且错误信息指出具体字段。

## 12. 测试策略

| 层 | 用例 |
|---|---|
| RunSpec | schema 校验、深度合并、非法字段 |
| PipelineRunner | 线性流程、依赖 DAG、skip、retry、阶段超时 |
| ProviderRegistry | 注册/查找/能力过滤 |
| FigureSkillProvider | native-svg 自检、第三方 provider 自检失败降级、事件 |
| ExperimentEngineProvider | athena adapter 对拍；mock mcts/external |
| Stage 泛化 | 自定义 stage 插入与移除 |

## 13. 与既有设计文档的关系

本文不推翻前序文档，而是把前序文档的固定服务收敛为：

| 前序文档概念 | 本文归口 |
|---|---|
| 五阶段硬编码 | RunSpec stages + Stage 注册表 |
| FigureDesigner | `FigureSkillProvider`（native-svg / fireworks / drawio / ink） |
| Athena 子集固定 | `ExperimentEngineProvider`（athena 默认） |
| Overleaf/Local/Markdown | `CompilerProvider`（overleaf / local / none） |
| TemplateKit 固定抓取 | `TemplateProvider`（curl 默认） |
| PaperReviewer 六项 | `GateRef[]` 可插拔 gate list |

## 14. 开放项

- 第三方 SVG skill 测试结果出来后，只改 `FigureSkillProvider` 适配层，不动核心。
- `RunSpec` 是否允许表达式条件（如 `depends_on` + `when`），首版可先不做，保持极简。
- Provider 热插拔（运行中切换）首版不做；只在 stage 开始前解析。
