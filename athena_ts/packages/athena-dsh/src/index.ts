/**
 * @athena/dsh — Athena 研究域作为 DSH cordis 插件。
 *
 * 关键纠偏：DSH 的 cordis 是 `@deepseek-ai/cordis`（vendor 的 cordis 4.0.1），
 * 工具经 `ctx.tools.register(ToolDefinition)` 注册，subagent 经
 * `ctx.subagents.start(name, request)` 启动——不是 npm `cordis` + 手工装配。
 * 本插件因此依赖 `@deepseek-ai/cordis` + `@deepseek-ai/dsh-tools`（+ 可选
 * `@deepseek-ai/dsh-subagent`），而非 npm `cordis`。
 */

import { existsSync, mkdirSync } from "node:fs"
import { join, resolve } from "node:path"
import type { Context } from "@deepseek-ai/cordis"
import {
  HypothesisSchema,
  LocalArtifactStore,
  LocalGitWorkspace,
  ResearchTree,
  type ArtifactRef,
  type Hypothesis,
} from "@athena/core"
import {
  DataScriptRunner,
  FixedFlowSupervisor,
  LocalExecutionRuntime,
  PlanInputSchema,
  PlanRunner,
  PlanStateSchema,
  parseResearchState,
  loadResearchState,
  type ResearchState,
  Scheduler,
  TrustedEvaluator,
  buildFinalReport,
  createPrepareWorkspace,
  runEvaluatorPlan,
  runPreparePlan,
  runValidationPlan,
  type AgentTurn,
  type PlanDecision,
  type SupervisorWorkers,
} from "@athena/research"

// ToolDefinition 形状来自 @deepseek-ai/dsh-tools（name/description/parameters/output/execute）。
// `import type {}` 加载 dsh-subagent 的 Context 增强（ctx.subagents）。
import type {
  JsonValue,
  ObjectJsonSchema,
  ToolCallView,
  ToolDefinition,
  ToolResult,
  ToolResultView,
  ToolRunContext,
} from "@deepseek-ai/dsh-tools"
import type {} from "@deepseek-ai/dsh-subagent"
import type { Agent } from "@deepseek-ai/dsh-agent"
import type { ContentBlock } from "@deepseek-ai/dsh-llm"

export interface AthenaResearchConfig {
  projectRoot?: string
  searchLimit?: number
  concurrency?: number
  ideatorCount?: number
  hypothesesPerIdeator?: number
  manualMode?: boolean
  autoValidate?: boolean
  direction?: "maximize" | "minimize"
  tolerance?: number
}

declare module "@deepseek-ai/cordis" {
  interface Context {
    researchStore: LocalArtifactStore
    researchTree: ResearchTree
    researchState: ResearchState
    researchScheduler: Scheduler
    researchEvaluator: TrustedEvaluator
    researchScriptRunner: DataScriptRunner
    researchGit: LocalGitWorkspace
    researchSupervisor: FixedFlowSupervisor
  }
}

function objectSchema(properties: Record<string, unknown>, required: string[]): Record<string, unknown> {
  return { type: "object", properties, required, additionalProperties: false }
}

/** 只读研究状态快照（phase/status/预算/manual_mode/Ideator 配置/SOTA/任务理解/验证）。 */
export function researchStatus(ctx: Context): Record<string, unknown> {
  return {
    phase: ctx.researchState.phase,
    status: ctx.researchState.status,
    search_limit: ctx.researchState.search_limit,
    concurrency: ctx.researchState.concurrency,
    ideator_count: ctx.researchState.ideator_count,
    hypotheses_per_ideator: ctx.researchState.hypotheses_per_ideator,
    manual_mode: ctx.researchState.manual_mode,
    task_understanding: ctx.researchState.task_understanding,
    validation: ctx.researchState.validation,
    sota_experiment_id: ctx.researchTree.bestExperimentId(),
    pending_hypotheses: ctx.researchTree.pendingHypotheses().length,
    running_plans: ctx.researchSupervisor.runningPlanIds,
  }
}

function textBlock(text: string): ContentBlock {
  return { type: "text", text }
}

function statusText(status: Record<string, unknown>): string {
  return Object.entries(status)
    .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
    .join("\n")
}

function statusBlocks(value: unknown): ContentBlock[] {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return [textBlock(JSON.stringify(value))]
  }
  return [textBlock(statusText(value as Record<string, unknown>))]
}

function toolOutput(): ToolDefinition["output"] {
  return {
    schema: { type: "object" },
    render: (_args: unknown, value: unknown) => [textBlock(JSON.stringify(value))],
    presentationMeta: (_args: unknown, value: JsonValue) => value,
  }
}

function toolResult(title: string) {
  return (_args: unknown, result: ToolResult): ToolResultView => ({
    card: "generic",
    title,
    content: result.meta === undefined ? result.content : statusBlocks(result.meta),
  })
}

function argRecord(args: unknown): Record<string, unknown> {
  if (typeof args === "object" && args !== null && !Array.isArray(args)) {
    return args as Record<string, unknown>
  }
  return {}
}

function argString(args: unknown, key: string): string | undefined {
  const value = argRecord(args)[key]
  return typeof value === "string" ? value : undefined
}

function argBoolean(args: unknown, key: string): boolean | undefined {
  const value = argRecord(args)[key]
  return typeof value === "boolean" ? value : undefined
}

function argNumber(args: unknown, key: string): number | undefined {
  const value = argRecord(args)[key]
  return typeof value === "number" ? value : undefined
}

function stringProp(description: string): Record<string, unknown> {
  return { type: "string", description }
}

function integerProp(description: string): Record<string, unknown> {
  return { type: "integer", description }
}

function booleanProp(description: string): Record<string, unknown> {
  return { type: "boolean", description }
}

function enumProp(values: string[], description: string): Record<string, unknown> {
  return { type: "string", enum: values, description }
}

interface ActionToolOptions {
  name: string
  description: string
  properties?: Record<string, unknown>
  required?: string[]
  callKind?: "read" | "execute" | "search"
  callTitle: string
  resultTitle?: string
  execute: (args: unknown) => Promise<unknown> | unknown
}

function actionTool(opts: ActionToolOptions): ToolDefinition {
  return {
    name: opts.name,
    description: opts.description,
    parameters: objectSchema(opts.properties ?? {}, opts.required ?? []),
    output: toolOutput(),
    presentCall: (args): ToolCallView => ({
      card: "generic",
      kind: opts.callKind ?? "execute",
      title: opts.callTitle,
      rawInput: Object.keys(argRecord(args)).length > 0 ? args : undefined,
    }),
    presentResult: toolResult(opts.resultTitle ?? opts.callTitle),
    execute: async (args) => {
      const value = await opts.execute(args)
      if (typeof value === "object" && value !== null && !Array.isArray(value)) return value
      return { result: value }
    },
  }
}

interface SupervisorToolOptions extends Omit<ActionToolOptions, "execute"> {
  run: (supervisor: FixedFlowSupervisor, args: Record<string, unknown>) => Promise<unknown> | unknown
}

function supervisorTool(ctx: Context, opts: SupervisorToolOptions): ToolDefinition {
  return actionTool({
    ...opts,
    execute: (args) => opts.run(ctx.researchSupervisor, argRecord(args)),
  })
}

/** 只读研究树快照：SOTA、实验列表与待选假设。 */
export function researchTreeSnapshot(ctx: Context): Record<string, unknown> {
  const data = ctx.researchTree.toDict()
  return {
    version: data.version,
    sota_id: data.sota_id,
    hypothesis_count: Object.keys(data.hypotheses).length,
    experiment_count: Object.keys(data.experiments).length,
    pending_hypotheses: ctx.researchTree.pendingHypotheses().map((hypothesis) => ({
      id: hypothesis.id,
      statement: hypothesis.statement,
      status: hypothesis.status,
    })),
    experiments: Object.entries(data.experiments).map(([experimentId, experiment]) => ({
      id: experimentId,
      hypothesis_id: experiment.hypothesis_id,
      kind: experiment.plan.kind,
      status: experiment.status,
    })),
  }
}

interface ToolRegistryLike {
  register(def: ToolDefinition): () => void
}

function optionalTools(ctx: Context): ToolRegistryLike | null {
  // 用 ctx.get("tools") 而不是 ctx.tools：npm cordis 的 proxy 对未提供
  // 服务名做属性访问会抛 "cannot get property ... without inject"。
  return (ctx.get("tools") as ToolRegistryLike | undefined) ?? null
}

interface RunToolDeps {
  projectRoot: string
  statePath: string
  direction: "maximize" | "minimize"
  execution: LocalExecutionRuntime
  runPlanTurn: SupervisorWorkers["runPlanTurn"]
}

/** 注册 Athena 研究域 Services + 两个模型可见工具（DSH 环境）。 */
export function researchPlugin(config: AthenaResearchConfig = {}) {
  return (ctx: Context): void => {
    const projectRoot = resolve(config.projectRoot ?? ".")
    const athena = join(projectRoot, ".athena")
    const treePath = join(athena, "research_tree.json")
    const statePath = join(athena, "state.json")
    const direction = config.direction ?? "maximize"
    const tolerance = config.tolerance ?? 0.0

    const store = new LocalArtifactStore(join(athena, "artifacts"))
    const git = new LocalGitWorkspace(join(athena, "repo"), join(projectRoot, "workspaces"), (b) => store.putBytes(b))
    const execution = new LocalExecutionRuntime()
    const scripts = new DataScriptRunner(store, join(athena, "runs"))
    const evaluator = new TrustedEvaluator(scripts)
    const tree = existsSync(treePath) ? ResearchTree.load(treePath) : new ResearchTree()
    const state = existsSync(statePath)
      ? loadResearchState(statePath)
      : parseResearchState({
          status: "RUNNING",
          phase: "PREPARE",
          search_limit: config.searchLimit ?? 10,
          concurrency: config.concurrency ?? 4,
          ideator_count: config.ideatorCount ?? 3,
          hypotheses_per_ideator: config.hypothesesPerIdeator ?? 2,
          manual_mode: config.manualMode ?? false,
        })

    const scheduler = new Scheduler()

    const deterministicPlanTurn: SupervisorWorkers["runPlanTurn"] = async (planId, planState) => {
      const planInput = await supervisor.planInput(planId)
      const runner = new PlanRunner(
        execution,
        store,
        evaluator,
        git,
        supervisor.workspace(planId),
        planInput.direction
      )
      return runner.runTurn(planId, planState, planInput)
    }

    const supervisor = new FixedFlowSupervisor({
      projectRoot,
      state,
      tree,
      store,
      git,
      scheduler,
      evaluatorRef: baselineEvaluatorRef(tree),
      direction,
      tolerance,
      autoValidate: config.autoValidate ?? false,
      workers: placeholderWorkers(),
    })

    ctx.provide("researchStore", store)
    ctx.provide("researchTree", tree)
    ctx.provide("researchState", state)
    ctx.provide("researchScheduler", scheduler)
    ctx.provide("researchEvaluator", evaluator)
    ctx.provide("researchScriptRunner", scripts)
    ctx.provide("researchGit", git)
    ctx.provide("researchSupervisor", supervisor)

    // 模型可见工具：只读快照 + 核心 Supervisor 动作 + research_run（启动固定流程）。
    // 本地对拍测试用 npm `cordis` 无 tools 服务，这里按可用性注册。
    const tools = optionalTools(ctx)
    if (tools) {
      const runDeps: RunToolDeps = {
        projectRoot,
        statePath,
        direction,
        execution,
        runPlanTurn: deterministicPlanTurn,
      }
      for (const def of researchToolDefinitions(ctx, runDeps)) {
        tools.register(def)
      }
    }

    function placeholderWorkers(): SupervisorWorkers {
      return {
        publish: async () => {},
        runPlanAgentTurn: async () => null,
        runIdeatorTurn: async () => [],
        runPreparePhase: async () => {
          throw new Error("PREPARE LLM worker requires DSH subagent wiring (call research_run with an agent context)")
        },
        runValidationPhase: async () => {
          throw new Error("VALIDATE LLM worker requires DSH subagent wiring (call research_run with an agent context)")
        },
        runPlanTurn: deterministicPlanTurn,
      }
    }
  }
}

function baselineEvaluatorRef(tree: ResearchTree): ArtifactRef | null {
  const baselines = tree.experiments("baseline")
  return baselines.length > 0 ? baselines[0]!.plan.run_config_ref : null
}

/** DSH 模型可见的研究域工具集：只读快照 + 核心 Supervisor 动作 + 流程启动。 */
function researchToolDefinitions(ctx: Context, deps: RunToolDeps): ToolDefinition[] {
  return [
    actionTool({
      name: "research_status",
      description: "Read the current Athena research phase, status, budgets, and SOTA experiment.",
      callKind: "search",
      callTitle: "查看 Athena 研究状态",
      resultTitle: "Athena 研究状态",
      execute: () => researchStatus(ctx),
    }),
    actionTool({
      name: "research_tree",
      description: "Read the Athena research tree: SOTA, experiments, and pending hypotheses.",
      callKind: "read",
      callTitle: "查看 Athena 研究树",
      resultTitle: "Athena 研究树",
      execute: () => researchTreeSnapshot(ctx),
    }),
    supervisorTool(ctx, {
      name: "research_hypotheses",
      description: "Read pending Athena hypotheses, SOTA experiment, and remaining search budget.",
      callKind: "read",
      callTitle: "查看 Athena 待选假设",
      resultTitle: "Athena 待选假设",
      run: (supervisor) => supervisor.readHypotheses(),
    }),
    supervisorTool(ctx, {
      name: "research_plans",
      description: "Read running and waiting Athena SEARCH plans with turn budgets.",
      callKind: "read",
      callTitle: "查看 Athena 执行计划",
      resultTitle: "Athena 执行计划",
      run: (supervisor) => supervisor.readPlans(),
    }),
    supervisorTool(ctx, {
      name: "research_propose_hypothesis",
      description: "Propose one Athena hypothesis under the current SOTA for later scheduling or manual selection.",
      properties: {
        statement: stringProp("Falsifiable hypothesis statement."),
        intervention: stringProp("Concrete change to test."),
        expected_effect: stringProp("Expected effect on the primary metric."),
      },
      required: ["statement", "intervention", "expected_effect"],
      callTitle: "提出 Athena 研究假设",
      resultTitle: "Athena 假设已提出",
      run: (supervisor, args) => supervisor.proposeHypothesis(args),
    }),
    supervisorTool(ctx, {
      name: "research_select_hypothesis",
      description: "In manual mode, select the next Athena hypothesis to execute (wakes the SEARCH loop).",
      properties: { hypothesis_id: stringProp("Hypothesis ID from research_hypotheses/research_tree.") },
      required: ["hypothesis_id"],
      callTitle: "选择下一个 Athena 假设",
      resultTitle: "Athena 假设已选择",
      run: (supervisor, args) => supervisor.selectNextHypothesis(argString(args, "hypothesis_id") ?? ""),
    }),
    supervisorTool(ctx, {
      name: "research_configure_search",
      description: "Set Athena SEARCH budget: search_limit and/or concurrency; resuming WAITING search when extended.",
      properties: {
        search_limit: integerProp("Total search attempts allowed (>= 1)."),
        concurrency: integerProp("Concurrent plan slots (>= 1)."),
      },
      callTitle: "配置 Athena 搜索预算",
      resultTitle: "Athena 搜索预算已更新",
      run: (supervisor, args) => {
        const patch: { search_limit?: number; concurrency?: number } = {}
        const searchLimit = argNumber(args, "search_limit")
        const concurrency = argNumber(args, "concurrency")
        if (searchLimit !== undefined) patch.search_limit = searchLimit
        if (concurrency !== undefined) patch.concurrency = concurrency
        return supervisor.configureSearch(patch)
      },
    }),
    supervisorTool(ctx, {
      name: "research_set_manual_mode",
      description: "Toggle Athena SEARCH scheduling between automatic priority queue and manual hypothesis selection.",
      properties: { manual: booleanProp("true = wait for research_select_hypothesis after each batch.") },
      required: ["manual"],
      callTitle: "切换 Athena 手动调度模式",
      resultTitle: "Athena 调度模式已切换",
      run: (supervisor, args) => supervisor.setManualMode(argBoolean(args, "manual") ?? false),
    }),
    supervisorTool(ctx, {
      name: "research_update_plan_budget",
      description: "Extend the turn_limit or patience of a WAITING Athena plan that exhausted its budget.",
      properties: {
        plan_id: stringProp("Waiting plan id from research_plans."),
        turn_limit: integerProp("New turn limit (must exceed turns already used)."),
        unlimited_turns: booleanProp("Remove the turn limit."),
        patience: integerProp("New patience between 1 and 5."),
      },
      required: ["plan_id"],
      callTitle: "更新 Athena 等待计划预算",
      resultTitle: "Athena 计划预算已更新",
      run: (supervisor, args) => {
        const patch: {
          plan_id?: unknown
          turn_limit?: number
          unlimited_turns?: boolean
          patience?: number
        } = { plan_id: argString(args, "plan_id") ?? "" }
        const turnLimit = argNumber(args, "turn_limit")
        const unlimitedTurns = argBoolean(args, "unlimited_turns")
        const patience = argNumber(args, "patience")
        if (turnLimit !== undefined) patch.turn_limit = turnLimit
        if (unlimitedTurns !== undefined) patch.unlimited_turns = unlimitedTurns
        if (patience !== undefined) patch.patience = patience
        return supervisor.updateWaitingPlanBudget(patch)
      },
    }),
    supervisorTool(ctx, {
      name: "research_set_phase_decision",
      description: "Move the interactive Athena run to SEARCH or VALIDATE.",
      properties: { decision: enumProp(["SEARCH", "VALIDATE"], "Phase to enter.") },
      required: ["decision"],
      callTitle: "设置 Athena 阶段决策",
      resultTitle: "Athena 阶段决策已设置",
      run: (supervisor, args) => {
        const decision = argString(args, "decision")
        if (decision !== "SEARCH" && decision !== "VALIDATE") throw new Error("decision must be 'SEARCH' or 'VALIDATE'")
        return supervisor.setPhaseDecision(decision)
      },
    }),
    supervisorTool(ctx, {
      name: "research_guidance",
      description: "Record human guidance that is frozen only into later Athena Plan inputs.",
      properties: {
        text: stringProp("Guidance text for later plans."),
        scope: enumProp(["next", "persistent"], "next = one plan only; persistent = all later plans."),
      },
      required: ["text", "scope"],
      callTitle: "记录 Athena 研究指导",
      resultTitle: "Athena 研究指导已记录",
      run: (supervisor, args) => {
        const scope = argString(args, "scope")
        if (scope !== "next" && scope !== "persistent") throw new Error("scope must be 'next' or 'persistent'")
        return supervisor.recordGuidance(argString(args, "text") ?? "", scope)
      },
    }),
    supervisorTool(ctx, {
      name: "research_task_understanding",
      description: "Record the supervisor's structured task understanding (title/dataset/target/task_type/primary_metric/direction/evaluation_plan).",
      properties: {
        title: stringProp("Short task title."),
        dataset: stringProp("One-line dataset description."),
        target: stringProp("Target column and type."),
        task_type: stringProp("classification | regression | vision | generation | other"),
        primary_metric: stringProp("Lowercased metric name."),
        direction: stringProp("maximize or minimize."),
        evaluation_plan: stringProp("How the primary metric is computed."),
      },
      callTitle: "记录 Athena 任务理解",
      resultTitle: "Athena 任务理解已记录",
      run: (supervisor, args) => supervisor.recordTaskUnderstanding(args),
    }),
    supervisorTool(ctx, {
      name: "research_pause",
      description: "Pause new Athena agent dispatch while retaining unfinished work.",
      callTitle: "暂停 Athena 研究",
      resultTitle: "Athena 研究已暂停",
      run: (supervisor) => supervisor.pause(),
    }),
    supervisorTool(ctx, {
      name: "research_resume",
      description: "Resume Athena agent dispatch after pause or manual selection.",
      callTitle: "恢复 Athena 研究",
      resultTitle: "Athena 研究已恢复",
      run: (supervisor) => supervisor.resume(),
    }),
    supervisorTool(ctx, {
      name: "research_stop",
      description: "Stop new Athena dispatch and wait for in-flight work and turn settlement to finish.",
      callTitle: "停止 Athena 研究",
      resultTitle: "Athena 研究已停止",
      run: (supervisor) => supervisor.requestStop(),
    }),
    supervisorTool(ctx, {
      name: "research_validate",
      description: "Transition SEARCH to VALIDATE and run frozen-SOTA validation to completion.",
      callTitle: "运行 Athena 验证阶段",
      resultTitle: "Athena 验证阶段",
      run: async (supervisor) => {
        await supervisor.setPhaseDecision("VALIDATE")
        return { status: supervisor.state.status }
      },
    }),
    actionTool({
      name: "research_report",
      description: "Render the deterministic Athena Markdown research report from the tree and validation result.",
      callKind: "read",
      callTitle: "生成 Athena 研究报告",
      resultTitle: "Athena 研究报告",
      execute: () => ({ report: buildFinalReport(ctx.researchTree, ctx.researchState.validation) }),
    }),
    runTool(ctx, deps),
  ]
}

function runTool(ctx: Context, deps: RunToolDeps): ToolDefinition {
  return {
    name: "research_run",
    description: "Start the fixed Athena research flow (PREPARE → SEARCH → VALIDATE).",
    parameters: objectSchema(
      {
        task: stringProp("Research task to execute. Defaults to a generic Athena research run."),
        task_understanding: {
          type: "object",
          description: "Optional structured task understanding (title/dataset/target/task_type/primary_metric/direction/evaluation_plan).",
        },
      },
      []
    ),
    output: toolOutput(),
    presentCall: (args): ToolCallView => ({
      card: "generic",
      kind: "execute",
      title: "运行 Athena 研究流程",
      rawInput: readTask(args),
    }),
    presentResult: toolResult("Athena 研究流程已启动"),
    async execute(args, exec: ToolRunContext) {
      const task = readTask(args)
      const understanding = argRecord(args)["task_understanding"]
      if (typeof understanding === "object" && understanding !== null && !Array.isArray(understanding)) {
        await ctx.researchSupervisor.recordTaskUnderstanding(understanding as Record<string, unknown>)
      }
      // DSH subagent `start` 强制要求 parent: Agent；拿到 exec.agent 后才能把
      // 占位 worker 换成真实 LLM worker。非 agent 驱动（或本地测试）保持占位。
      if (exec.agent) {
        ctx.researchSupervisor.setWorkers(
          dshWorkers(ctx, {
            parent: exec.agent,
            signal: exec.signal,
            task,
            direction: deps.direction,
            projectRoot: deps.projectRoot,
            statePath: deps.statePath,
            execution: deps.execution,
            runPlanTurn: deps.runPlanTurn,
          })
        )
      }
      await ctx.researchSupervisor.start()
      return researchStatus(ctx)
    },
  }
}

function readTask(args: unknown): string {
  return argString(args, "task")?.trim() || "Run the Athena research workflow (PREPARE → SEARCH → VALIDATE)."
}

/** DSH 组合 ``name: '@athena/dsh'`` 按此加载：loader 以 ``(ctx, config)`` 直接调用插件，
  * 因此默认导出必须是直接插件（而非 ``(config) => (ctx) => {}`` 工厂——cordis 会把工厂
  * 当 disposer 收集而不会执行其返回体）。内部转调工厂复用同一份装配逻辑。 */
export default function athenaDshPlugin(ctx: Context, config: AthenaResearchConfig = {}) {
  researchPlugin(config)(ctx)
}

/**
 * 用 DSH subagent 接线 LLM worker（替代占位）。
 *
 * ``parent`` 与 ``signal`` 来自驱动工具的 ``exec.agent``/``exec.signal``——subagent
 * ``start`` 强制要求 ``parent: Agent``，所以 worker 不能脱离一次 agent 驱动的工具调用
 * 独立后台运行。研究流程由某个 agent 调用 ``research_run`` 工具驱动：该工具的
 * ``execute(args, exec)`` 用 ``exec.agent``/``exec.signal`` 调本工厂，再把返回的
 * ``SupervisorWorkers`` 交给 supervisor（``FixedFlowSupervisor.setWorkers``）。
 *
 * 结构化结果经 ``outputSchema`` 请求、``run.result`` 的 ``structured`` 读取；文本经
 * ``result.output``（ContentBlock[]）。
 */
export interface DshWorkersOptions {
  parent: Agent
  signal: AbortSignal
  task: string
  direction: "maximize" | "minimize"
  projectRoot: string
  statePath: string
  execution: LocalExecutionRuntime
  /** 确定性 PlanRunner（打分），由调用方（插件）提供，不经 subagent。 */
  runPlanTurn: SupervisorWorkers["runPlanTurn"]
}

/**
 * 用 DSH subagent 接线 LLM worker（替代占位）。
 *
 * ``parent``/``signal`` 来自驱动工具的 ``exec.agent``/``exec.signal``。结构化结果经
 * ``outputSchema`` 请求、``run.result`` 的 ``structured`` 读取；多步 PREPARE/VALIDATE
 * 复用 @athena/research 的 ``runEvaluatorPlan``/``runPreparePlan``/``runValidationPlan``，
 * 每个 worker turn 是一个 subagent（自带 DSH 工具写文件），确定性打分仍走 runPlanTurn。
 */
export function dshWorkers(ctx: Context, opts: DshWorkersOptions): SupervisorWorkers {
  const { parent, signal } = opts

  async function spawnStructured<T>(
    prompt: string,
    outputSchema: Record<string, unknown>
  ): Promise<T | null> {
    const subagents = ctx.get("subagents")
    if (!subagents) throw new Error("DSH subagents service is not available")
    const provider = subagents.list()[0] ?? "spawn"
    const run = await subagents.start(provider, {
      prompt: [{ type: "text", text: prompt }] as ContentBlock[],
      parent,
      signal,
      outputSchema: outputSchema as unknown as ObjectJsonSchema,
    })
    try {
      const result = await run.result
      if (result.stopReason !== "completed") return null
      return (result.structured ?? null) as T | null
    } finally {
      await run.dispose()
    }
  }

  function jsonPrompt(sections: Array<string | null | undefined>): string {
    const body = sections.filter((section): section is string => typeof section === "string" && section !== "").join("\n\n")
    return `${body}\n\nReturn JSON only.`
  }

  function taskContext(): string {
    const lines = [`Task: ${opts.task}`]
    const understanding = ctx.researchState.task_understanding
    if (understanding !== null && Object.keys(understanding).length > 0) {
      lines.push("Task understanding:")
      for (const [key, value] of Object.entries(understanding)) {
        lines.push(`- ${key}: ${JSON.stringify(value)}`)
      }
    }
    return lines.join("\n")
  }

  function sotaContext(): string {
    const sotaId = ctx.researchTree.bestExperimentId()
    if (sotaId === null) return "SOTA: none (PREPARE has not completed)"
    const sota = ctx.researchTree.getExperiment(sotaId)
    const hypothesis = ctx.researchTree.getHypothesis(sota.hypothesis_id)
    return [
      `SOTA experiment: ${sotaId}`,
      `SOTA metric: ${sota.eval === null ? "unavailable" : sota.eval.primary}`,
      `SOTA hypothesis: ${hypothesis.statement}`,
      `SOTA intervention: ${hypothesis.intervention}`,
    ].join("\n")
  }

  function planPrompt(planId: string, state: { turns_used: number; turn_limit: number | null }): string {
    return jsonPrompt([
      "You are Athena's SEARCH Plan agent. Inspect the plan workspace and decide whether to continue editing, submit the current best result, or abandon the plan.",
      taskContext(),
      sotaContext(),
      [
        `Plan id: ${planId}`,
        `Turns used: ${state.turns_used}`,
        `Turn limit: ${state.turn_limit ?? "unlimited"}`,
        `Direction: ${opts.direction}`,
      ].join("\n"),
    ])
  }

  const planDecisionSchema = {
    type: "object",
    properties: {
      decision: { type: "string", enum: ["continue", "submit", "abandon"] },
      reason: { type: "string" },
    },
    required: ["decision", "reason"],
  }

  const hypothesisBatchSchema = {
    type: "object",
    properties: {
      hypotheses: {
        type: "array",
        items: {
          type: "object",
          properties: {
            statement: { type: "string" },
            intervention: { type: "string" },
            expected_effect: { type: "string" },
          },
          required: ["statement", "intervention", "expected_effect"],
        },
      },
    },
    required: ["hypotheses"],
  }

  function planDecisionAgent(): AgentTurn {
    return {
      turn: (prompt) => spawnStructured<PlanDecision>(prompt, planDecisionSchema),
    }
  }

  return {
    publish: async () => {},
    runPlanTurn: opts.runPlanTurn,
    runPlanAgentTurn: async (planId, state) => {
      let contextLines: string[] = []
      try {
        const input = await ctx.researchSupervisor.planInput(planId)
        if (input.hypothesis !== null) {
          contextLines = [
            `Hypothesis: ${input.hypothesis.statement}`,
            `Intervention: ${input.hypothesis.intervention}`,
            `Expected effect: ${input.hypothesis.expected_effect}`,
            `Reference experiment: ${input.reference_experiment_id ?? "none"}`,
            `Reference metric: ${input.reference_metric ?? "none"}`,
            `Tolerance: ${input.tolerance}`,
            input.human_context ? `Human guidance: ${input.human_context}` : "",
          ].filter(Boolean)
        }
      } catch {
        // 缺失/损坏的 plan context 会由确定性打分链路再次报错；这里降级为最小提示。
      }
      const lines = [
        planPrompt(planId, state),
        ...(contextLines.length > 0 ? ["", "Frozen plan input:", ...contextLines] : []),
        "Current plan state:",
        JSON.stringify(state),
      ]
      return spawnStructured<PlanDecision>(lines.join("\n"), planDecisionSchema)
    },
    runIdeatorTurn: async (count) => {
      const snapshot = await ctx.researchSupervisor.readHypotheses()
      const pending = snapshot["pending"]
      const pendingLines = Array.isArray(pending)
        ? pending.map((item) => {
            const h = item as { id: string | null; statement: string }
            return `- ${h.id ?? "(unregistered)"}: ${h.statement}`
          })
        : []
      const prompt = jsonPrompt([
        "You are Athena's Ideator. Propose new falsifiable hypotheses that improve the primary metric over the current SOTA.",
        taskContext(),
        sotaContext(),
        `Propose exactly ${count} hypotheses.`,
        pendingLines.length > 0 ? `Already pending (do not duplicate):\n${pendingLines.join("\n")}` : null,
      ])
      const batch = await spawnStructured<{ hypotheses: Array<Record<string, unknown>> }>(
        prompt,
        hypothesisBatchSchema
      )
      const parsed = HypothesisSchema.array().safeParse(batch?.hypotheses ?? [])
      return (parsed.success ? parsed.data : []) as Hypothesis[]
    },
    runPreparePhase: async () => {
      const workspace = await createPrepareWorkspace({
        git: ctx.researchGit,
        projectRoot: opts.projectRoot,
        state: ctx.researchState,
        statePath: opts.statePath,
      })

      const evaluatorPrompt = jsonPrompt([
        "You are Athena's evaluator agent. In the evaluator workspace, write `metric.json` declaring `eval_script` and a Python evaluator that scores a predictions directory, then return submit.",
        "The evaluator must sit next to non-empty labels (labels.csv or labels/).",
        taskContext(),
        `Evaluator workspace: ${join(opts.projectRoot, "workspaces", "evaluator")}`,
      ])

      const evaluatorAgent = planDecisionAgent()
      const evaluatorDir = join(opts.projectRoot, "workspaces", "evaluator")
      mkdirSync(evaluatorDir, { recursive: true })
      const evaluatorRef = await runEvaluatorPlan({
        agent: evaluatorAgent,
        scripts: ctx.researchScriptRunner,
        store: ctx.researchStore,
        evaluatorDir,
        task: evaluatorPrompt,
        maxTurns: 20,
      })
      const preparePrompt = jsonPrompt([
        "You are Athena's PREPARE agent. Write `experiment.json` (version 1, commands, outputs including predictions and report) and the baseline experiment code in the workspace, then return submit.",
        "Commands must produce predictions and a non-empty report so the trusted scorer can freeze a baseline.",
        taskContext(),
        `Workspace: ${workspace.path}`,
      ])
      const treeRef = await ctx.researchStore.putText(JSON.stringify(ctx.researchTree.toDict()))
      return runPreparePlan({
        agent: planDecisionAgent(),
        evaluator: ctx.researchEvaluator,
        git: ctx.researchGit,
        workspace,
        execution: opts.execution,
        store: ctx.researchStore,
        evaluatorRef,
        treeRef,
        task: preparePrompt,
        maxTurns: 20,
      })
    },
    runValidationPhase: async (commit, metric) => {
      const evaluatorRef = ctx.researchSupervisor.evaluatorRef
      if (evaluatorRef === null) throw new Error("VALIDATE requires a frozen evaluator")
      await ctx.researchGit.init(undefined, ".gitignore", ".venv/\n")
      const workspace = await ctx.researchGit.create(commit, "athena/validate")
      const treeRef = await ctx.researchStore.putText(JSON.stringify(ctx.researchTree.toDict()))
      const contextRef = await ctx.researchStore.putText(
        JSON.stringify({ plan_id: "validate", task: opts.task, tree_ref: treeRef })
      )
      const validationInput = PlanInputSchema.parse({
        evaluator_ref: evaluatorRef,
        tree_ref: treeRef,
        direction: opts.direction,
      })
      const prompt = jsonPrompt([
        "You are Athena's validate agent. Independently validate the frozen SOTA in the validation workspace.",
        taskContext(),
        sotaContext(),
        [
          `SOTA commit: ${commit}`,
          `Trusted reference metric: ${metric}`,
          `Validation workspace: ${workspace.path}`,
        ].join("\n"),
        "Write `experiment.json` (version 1, commands, outputs with predictions) and any runtime-only prediction code, then return submit.",
        "Do not change model, data, features, preprocessing, training, or final-label access.",
      ])
      let firstTurn = true
      const validateAgent: AgentTurn = {
        turn: (runtimePrompt) => {
          const effectivePrompt = firstTurn ? prompt : runtimePrompt
          firstTurn = false
          return spawnStructured<PlanDecision>(effectivePrompt, planDecisionSchema)
        },
      }
      return runValidationPlan({
        agent: validateAgent,
        testScore: metric,
        direction: opts.direction,
        maxTurns: 20,
        runFinalTest: async () => {
          const planState = PlanStateSchema.parse({
            kind: "VALIDATE",
            context_ref: contextRef,
            turns_used: 0,
            turn_limit: null,
          })
          const runner = new PlanRunner(
            opts.execution,
            ctx.researchStore,
            ctx.researchEvaluator,
            ctx.researchGit,
            workspace,
            opts.direction
          )
          const outcome = await runner.runTurn("validate", planState, validationInput)
          if (outcome.kind !== "scored" || outcome.metric === null) {
            throw new Error(outcome.error ?? `final-test failed: ${outcome.kind}`)
          }
          return outcome.metric
        },
      })
    },
  }
}
