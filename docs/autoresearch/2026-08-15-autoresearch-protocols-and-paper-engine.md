# AutoResearch 阶段协议与 Paper Engine 详细设计

日期：2026-08-15
状态：设计文档（待实现）
上游：`2026-08-15-autoresearch-detailed-design.md`、`2026-08-15-autoresearch-ts-plugin-design.md`

---

## 0. 2026-08-20 集成说明

本文的实验边界、证据纪律、模板/编译降级、Reviewer 和 Packaging 规则继续有效。
ML v1 不要求把每个概念实现为独立 Service 或完整 TypeScript interface：

- AutoResearch 通过现有 candidate/records-to-paper handoff 执行论文流程；
- 必须程序化的部分仍是 evidence 抽取、`verify-trace`、编译和确定性图表；
- Writer、Reviewer、rubric 和修订纪律优先由 DSH subagent + handoff 承载；
- Python Athena 的启动与恢复由 Athena 插件封装，AutoResearch 只消费 evidence 引用。

控制平面和代码范围以
[`2026-08-20-autoresearch-ml-control-plane-design.md`](2026-08-20-autoresearch-ml-control-plane-design.md)
为准。

## 1. 本文目的

补齐详细设计中“接口边界”和“论文引擎”两个尚未完全展开的部分：

- AutoResearch 与 Athena Experiment 子集之间的**阶段协议**。
- Ideation 与 HypothesisPool 的**入池协议**。
- Paper Engine（TemplateKit / Overleaf / LatexBuilder / Composer / Reviewer / Packaging）的**内部契约**。
- 事件 payload、错误码、BDD 验收场景。

## 2. AutoResearch ↔ Athena Experiment 协议

### 2.1 适配接口

AutoResearch 不直接操作 `FixedFlowSupervisor` 内部，只经 `AthenaExperimentEngine` 适配：

```ts
interface AthenaExperimentEngine {
  // 生命周期
  start(): Promise<void>                       // 等价 researchSupervisor.start()
  recover(state?: unknown): Promise<void>      // 等价 researchSupervisor.recover()
  stop(): Promise<void>                        // 等价 researchSupervisor.stop()

  // 只读快照
  phase(): "PREPARE" | "SEARCH" | "VALIDATE" | "COMPLETED"
  status(): "RUNNING" | "WAITING" | "COMPLETED" | "STOPPED" | "FAILED"
  sota(): string | null

  // 候选注入：EXPERIMENT 开始时，把池候选写入 ResearchTree
  registerCandidates(hypotheses: Hypothesis[]): Promise<string[]>

  // 结算订阅：AutoResearch 收到 settlement 后回写池
  onSettlement(handler: (event: SettlementEvent) => Promise<void>): () => void
}
```

`SettlementEvent`：

```ts
{
  hypothesis_id: string
  experiment_id: string
  outcome: "WIN" | "DRAW" | "LOSS" | "INCONCLUSIVE"
  metric: number | null
  reference_metric: number | null
  direction: "maximize" | "minimize"
  sota_after: string | null
}
```

### 2.2 阶段映射

| AutoResearch | Athena `ResearchState` | 进入条件 | 退出条件 |
|---|---|---|---|
| `EXPERIMENT` | `PREPARE` | 池中有 `QUEUED` 候选 | baseline 已建立 |
| `EXPERIMENT` | `SEARCH` | baseline 后 | search 预算耗尽或人工 VALIDATE |
| `EXPERIMENT` | `VALIDATE` | SOTA 存在 | `COMPLETED` |
| `WRITING` | `COMPLETED` | 实验完成 | — |

### 2.3 结算事件如何产生

- 当前 Python/TS `Supervisor._settle_plan` 不对外发 settlement 事件；AutoResearch 在 `AthenaExperimentEngine` 内**包装 `publish`/`runPlanTurn` 的返回路径**，从 `PlanTurnResult` 与 `_settle_plan` 之后的状态差量中推导 settlement。
- 设计上更干净的做法（实现期二选一）：
  - **A（推荐）**：在 `@athena/research` 的 `FixedFlowSupervisor` 增加一个 `settlement` 回调/事件（对现有行为零影响，默认空回调）。
  - **B**：AutoResearch 轮询 `ResearchTree` 的 diff（不推荐，时序差）。
- 无论 A/B，AutoResearch 只消费，不修改 Athena 内部。

### 2.4 失败与恢复

- `researchSupervisor.recover()` 成功后，AutoResearch 必须 `hypothesisPool.reconcile(tree)`，再继续等待结算。
- 若 Athena 状态为 `WAITING` 且 `search_limit` 耗尽，AutoResearch 触发 `set_phase_decision("VALIDATE")` 或按预算门置 `WAITING`。

## 3. Ideation ↔ HypothesisPool 入池协议

### 3.1 `IdeationPort`

```ts
interface IdeationPort {
  runBatch(input: IdeationInput): Promise<IdeationBatchResult>
}

interface IdeationInput {
  target: number                      // 本轮最少需要的候选数
  parent_experiment_id?: string       // 缺省 = 当前 SOTA
  budget: { max_ideas: number; max_tokens: number }
  context_refs: ArtifactRef[]
}

interface IdeationBatchResult {
  candidates: CandidateRecord[]       // 已过门禁/被拒，待入池
  eda_request?: string                // 需要动态 EDA 时非空
  tokens_used: number
}

interface CandidateRecord {
  hypothesis: Hypothesis
  origin: "live_eda_ideator" | "idea_generation_pipeline" | "manual" | "paper_revision"
  generation_strategy: string | null
  gate_summary: GateSummary | null
}
```

### 3.2 入池算法

```
for c in result.candidates:
  if pool.findDuplicates(c.hypothesis.statement + c.hypothesis.intervention, 0.8).length > 0:
    pool.upsert(duplicate_of, updated_at)
    continue
  if c.gate_summary is null or c.gate_summary.verdict == "PASS":
    pool.upsert({...c, pool_status: "QUEUED"})
  else:
    pool.upsert({...c, pool_status: "REJECTED", gate_summary: c.gate_summary})
```

### 3.3 与动态 EDA 的交互

- 若 `IdeationBatchResult.eda_request` 非空，AutoResearch 在 `EXPERIMENT` 之前或 `IDEATION` 阶段末尾调用 `runDataTurn(request)`（复用 `@athena/dsh` 的 Data Agent 接线）。
- Data Agent 返回 `EdaResult.summary` 后，本轮 Ideation 重新执行一次，再入池。

## 4. TemplateKit 详细设计

### 4.1 Venue 注册表

```ts
const TEMPLATE_REGISTRY: Record<TemplateVenue, {
  name: string
  url: string                  // 官方模板下载 URL（白名单内）
  format: "zip" | "tar.gz"
  rootDir: string              // 解压后模板根目录名
  mainFile: string             // 入口 .tex
  styleFiles: string[]         // 只读 class/style 文件
  editableFiles: string[]      // 允许 LLM 编辑的文件
}>
```

示例（URL 为实现期确认的官方地址）：

| venue | url 形态 | mainFile | 说明 |
|---|---|---|---|
| `iclr2026` | `https://iclr.cc/...` 官方模板 zip | `main.tex` | 匿名化规则启用 |
| `icml2026` | `https://icml.cc/...` 官方模板 zip | `main.tex` | 匿名化规则启用 |
| `neurips2026` | `https://neurips.cc/...` 官方模板 zip | `main.tex` | 待官方发布 |
| `plain_latex` | 内置骨架 | `main.tex` | 非会议 |
| `markdown` | 内置骨架 | `paper_draft.md` | 无 LaTeX |

### 4.2 fetch 流程

```
fetch(venue):
  entry = TEMPLATE_REGISTRY[venue]
  tmp = templates/.tmp/<venue>-<ts>.zip
  run: curl -L --fail --silent --show-error <entry.url> -o <tmp>
    capture stdout/stderr 原文
  on success:
    extract(tmp, templates/<venue>/current)
    sha256(file tree) -> fetch.json
    rm -rf templates/<venue>/cache
    cp -r templates/<venue>/current templates/<venue>/cache
    return {dir: current, source: "network", sha256}
  on failure:
    if cache exists: return {dir: cache, source: "cache"}
    else: throw TemplateFetchError(venue, url, stderr)
```

约束：

- URL 必须命中注册表白名单，禁止模板 URL 来自 LLM 输出。
- `curl` 超时（连接 15s，整体 120s），失败日志全文进入控制台事件。
- 每次启动必拉取，不做“过期判断”；拉取失败才回退 cache。

## 5. Overleaf 官方 API 设计

### 5.1 配置

- `OVERLEAF_API_TOKEN`：必填，只从环境变量读取。
- `OVERLEAF_API_BASE_URL`：可选，默认官方地址。

### 5.2 API 方法映射

| 方法 | HTTP（概念） | 行为 |
|---|---|---|
| `healthCheck()` | `GET /api/v2/user` | 校验 token；失败抛 `OverleafAuthError` |
| `createProject(name)` | `POST /api/v2/projects` | 创建项目，返回 project id |
| `syncFiles(projectId, files)` | `PUT /api/v2/projects/{id}/files` | 全量同步 `paper/<run_id>/` 到项目 |
| `compile(projectId)` | `POST /api/v2/projects/{id}/compile` | 触发云端编译，返回 compile id |
| `getPdf(projectId, compileId)` | `GET /api/v2/projects/{id}/output/pdf` | 下载 PDF，写 ArtifactStore |
| `getLogs(projectId, compileId)` | `GET /api/v2/projects/{id}/output/log` | stdout/stderr 原文 |

> 精确路径以 Overleaf 官方 API 文档为准；本设计只固定方法契约与错误分类，不固定 URL 字符串。

### 5.3 重试与降级

- `healthCheck` 失败：不重试，直接降级 `local`。
- `compile` 网络错误/429：指数退避重试 3 次（1s/2s/4s）；仍失败降级 `local`。
- `local` 不可用（无 TeX）：降级 `none`，只维护 Markdown draft。
- 每次降级写 `autoresearch/paper_engine_degraded` 事件，包含原因与目标模式。

## 6. LatexBuilder 详细设计

| 编译器 | 命令 |
|---|---|
| `latexmk` | `latexmk -pdf -interaction=nonstopmode -halt-on-error` |
| `tectonic` | `tectonic -X compile main.tex` |
| `pdflatex` | `pdflatex -interaction=nonstopmode -halt-on-error main.tex` |

- 工作目录：`paper/<run_id>/`。
- 输出：
  - PDF：`paper/<run_id>/main.pdf` 或打包阶段复制为 `packaging/<run_id>/paper.pdf`。
  - 日志：`.athena/autoresearch/logs/compile/<run_id>-<timestamp>.log`。
- 控制台输出：子进程 stdout/stderr 原文 + 日志 ArtifactRef，发布为 `paper_compile_output` 事件。
- 编译失败判定：退出码非 0 或 PDF 缺失。

## 7. PaperComposer 与 PaperReviewer 详细设计

### 7.1 PaperComposer 写作契约

输入已定义于详细设计 §5.6。写作过程：

1. **骨架复制**：把模板 `main.tex` 与 style 文件复制进 `paper/<run_id>/`，style 只读。
2. **逐 section 生成**：Paper Writer subagent 按序写 `sections/*.tex`，每次只写一个 section，并在 `paper_draft.md` 追加对应 Markdown 段落。
3. **证据引用**：每个数字、每个图表都必须使用 `\ref` 或注释形式 `% evidence: <artifact_ref>` 标注来源；`PaperReviewer` 据此做可追溯检查。
4. **文献引用**：`bibliography.bib` 由文献检索阶段结果生成，不允许 Paper Writer 编造 DOI/URL。
5. **首轮编译**：写作完成后立即编译一次，得到基线 `compileResult`。

### 7.2 PaperReviewer 的 rubric 实现

```ts
interface ReviewInput {
  paperDir: string
  evidence: PaperEvidenceBundle
  spec: PaperSpec
  compileResult: CompileResult
}

interface GateResult {
  pass: boolean
  blocking_factor: string | null
  item_scores: Array<{
    item: string
    score: 0 | 1
    evidence: string
  }>
  feedback: string[]
}
```

`compile_ok` 不通过时：

- 不进入内容修订循环；
- 进入编译修复循环（见详细设计 §8.4）；
- `blocking_factor` 固定为 `"compile_ok"`，`evidence` 取编译日志最后 2000 字符（脱敏后）。

### 7.3 Paper Writer / Reviewer 的 subagent 输出契约

- Paper Writer：`outputSchema = { sections: [{ name, content }], draft_sections: [{ name, markdown }] }`。
- Paper Reviewer：`outputSchema = GateResult`（zod schema 严格校验）。
- 两者都是 DSH subagent；`parent` 来自驱动 `autoresearch_run` 的 agent，`signal` 来自工具执行。

## 8. Packaging 详细设计

`PackagingService.package(runId, state, tree, pool, evidence, compileResult)`：

产出：

```
packaging/<run_id>/
  paper.pdf                      # 编译成功时
  paper_draft.md                 # 始终
  paper_sources.zip              # paper/<run_id> 全部源文件
  experiment_evidence.json       # 见下
  reproducibility_bundle/
    sota_diff.patch
    evaluator_ref.txt
    data_scripts_ref.txt
    run_logs_ref.txt
    pool_snapshot.json
```

`experiment_evidence.json`：

```jsonc
{
  "version": 1,
  "run_id": "ar_...",
  "claims": [
    {
      "claim": "method A improves primary metric from 0.80 to 0.86",
      "section": "results",
      "experiment_id": "exp_hyp_1a2b",
      "pool_id": "hyp_1a2b",
      "artifact_ref": "artifact:...",
      "primary_before": 0.80,
      "primary_after": 0.86,
      "direction": "maximize"
    }
  ],
  "negative_results": [
    { "pool_id": "hyp_3c4d", "experiment_id": "exp_hyp_3c4d", "reason": "DRAW" }
  ]
}
```

## 9. 事件 payload 契约（zod 骨架）

```ts
const PhaseStartedEvent = z.object({
  type: z.literal("autoresearch/phase_started"),
  run_id: z.string(), phase: AutoResearchPhase, round: z.number().int(),
})
const PhaseCompletedEvent = z.object({
  type: z.literal("autoresearch/phase_completed"),
  run_id: z.string(), phase: AutoResearchPhase, gate_result: z.unknown().optional(),
})
const GateFailedEvent = z.object({
  type: z.literal("autoresearch/gate_failed"),
  run_id: z.string(), phase: AutoResearchPhase,
  blocking_factor: z.string(), feedback: z.array(z.string()),
})
const BudgetExhaustedEvent = z.object({
  type: z.literal("autoresearch/budget_exhausted"),
  run_id: z.string(), phase: AutoResearchPhase, budget: z.string(),
})
const PaperVersionEvent = z.object({
  type: z.literal("autoresearch/paper_version"),
  run_id: z.string(), version: z.number().int(),
  draft_ref: ArtifactRefSchema, pdf_ref: ArtifactRefSchema.optional(),
  gate_result: z.unknown().optional(),
})
const PaperCompileOutputEvent = z.object({
  type: z.literal("paper_compile_output"),
  run_id: z.string(), ok: z.boolean(),
  console_text: z.string(), log_ref: ArtifactRefSchema, compiler: z.string(),
})
const PoolChangedEvent = z.object({
  type: z.literal("hypothesis_pool_changed"),
  run_id: z.string(), pool_id: z.string(), pool_status: PoolStatusSchema,
})
const PaperEngineDegradedEvent = z.object({
  type: z.literal("autoresearch/paper_engine_degraded"),
  run_id: z.string(), from: z.enum(["overleaf", "local", "none"]),
  to: z.enum(["overleaf", "local", "none"]), reason: z.string(),
})
```

## 10. 错误码

| 错误码 | 含义 | 处理 |
|---|---|---|
| `TEMPLATE_FETCH_FAILED` | curl 失败且无缓存 | INTAKE 置 WAITING，返回控制台错误 |
| `TEMPLATE_URL_NOT_ALLOWED` | 非白名单 URL | 拒绝执行 |
| `OVERLEAF_AUTH_FAILED` | token 无效 | 降级 local |
| `OVERLEAF_RATE_LIMITED` | 429 | 退避重试 3 次后降级 |
| `LATEX_COMPILE_FAILED` | 本机/云端编译不过 | 编译修复循环，只受时间限制 |
| `BUDGET_EXHAUSTED` | 阶段预算耗尽 | 置 WAITING |
| `TIME_LIMIT_REACHED` | 超过 `project_time_limit` | 置 WAITING，冻结现场 |
| `ATHENA_EXPERIMENT_FAILED` | Athena 子集 FAILED | AutoResearch FAILED 并记录原因 |
| `POOL_RECONCILE_FAILED` | 池对账失败 | 告警，不阻塞，下次重试 |
| `PAPER_GATE_FAILED` | 内容质量闸未过 | 按反馈修订，`max_paper_rounds` 耗尽则冻结 |

## 11. BDD 验收场景

### 场景 1：正常全流程

```
Given 任务、模板 iclr2026、Overleaf token、本机 TeX 可用
When AutoResearch 启动
Then 依次经过 INTAKE→IDEATION→EXPERIMENT→WRITING→REFINEMENT→PACKAGING→COMPLETED
And 产出 paper.pdf 与 paper_draft.md
And 阴性结果出现在 limitations.tex
```

### 场景 2：模板抓取失败

```
Given 会议官网不可达
And 上次缓存存在
When AutoResearch 启动
Then TemplateKit 回退 cache
And 事件包含 source=cache
```

```
Given 会议官网不可达
And 无缓存
When AutoResearch 启动
Then 状态 INTAKE/WAITING
And 控制台返回 curl 错误原文
```

### 场景 3：编译失败自修复

```
Given 论文 LaTeX 存在语法错误
And project_time_limit 未耗尽
When REFINEMENT 阶段执行
Then 每次编译失败都发布 paper_compile_output（含 stdout/stderr）
And 修复循环持续到编译成功
And 不消耗 max_paper_rounds
```

```
Given 论文 LaTeX 持续编译失败
And project_time_limit 耗尽
When REFINEMENT 阶段执行
Then 状态 WAITING
And 冻结当前源文件与完整编译日志
And 最终状态可见 compile_ok=false
```

### 场景 4：Overleaf 降级

```
Given OVERLEAF_API_TOKEN 无效
When WRITING 阶段首次编译
Then 发布 paper_engine_degraded(overleaf→local)
And 使用本机 TeX 编译
```

### 场景 5：池对账与崩溃恢复

```
Given 进程在 EXPERIMENT 结算后崩溃
When AutoResearch 重启
Then StateStore 加载 state
And HypothesisPool.reconcile(tree) 恢复池状态
And EXPERIMENT 从 Athena recover 续跑
```

## 12. 实现顺序建议

1. schemas + StateStore + HypothesisPool（纯模型/存储层）
2. TemplateKit + LatexBuilder（可独立测试）
3. OverleafConnector（mock 官方 API 测试）
4. AthenaExperimentEngine 适配 + settlement 回调
5. AutoResearchSupervisor 阶段机 + 事件
6. PaperComposer / PaperReviewer / Packaging
7. plugin + tools + 端到端 fake subagent 集成

## 13. 开放项

- Overleaf 官方 API 的精确端点与分页/幂等字段。
- ICLR/ICML 官方模板 URL 与解压后目录名（实现期确认并写入注册表）。
- settlement 回调落在 `@athena/research` 的哪个扩展点（回调 vs 事件）。
- `experiment_evidence.json` 的 claim 提取由规则还是 LLM 生成（建议规则 + LLM 复核）。
