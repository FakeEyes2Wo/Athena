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
  PlanRunner,
  ResearchState,
  Scheduler,
  TrustedEvaluator,
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
  ObjectJsonSchema,
  ToolCallView,
  ToolDefinition,
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

/** 只读研究状态快照（phase/status/预算/manual_mode/Ideator 配置/SOTA）。 */
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
    sota_experiment_id: ctx.researchTree.bestExperimentId(),
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
    const execution = new LocalExecutionRuntime(projectRoot, projectRoot)
    const scripts = new DataScriptRunner(store, join(athena, "runs"))
    const evaluator = new TrustedEvaluator(scripts)
    const tree = existsSync(treePath) ? ResearchTree.load(treePath) : new ResearchTree()
    const state = existsSync(statePath)
      ? ResearchState.load(statePath)
      : new ResearchState({
          status: "RUNNING",
          phase: "PREPARE",
          search_limit: config.searchLimit ?? 10,
          concurrency: config.concurrency ?? 4,
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
        {
          project_root: projectRoot,
          workspace_root: supervisor.workspacePath(planId),
          environment_root: projectRoot,
          experiment_id: planId,
        },
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

    // 模型可见工具：research_status / research_tree（只读）+ research_run（启动固定流程）。
    // 本地对拍测试用 npm `cordis` 无 tools 服务，这里按可用性注册。
    const tools = optionalTools(ctx)
    if (tools) {
      tools.register(statusTool(ctx))
      tools.register(treeTool(ctx))
      tools.register(runTool(ctx, {
        projectRoot,
        direction,
        execution,
        runPlanTurn: deterministicPlanTurn,
      }))
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

function statusTool(ctx: Context): ToolDefinition {
  return {
    name: "research_status",
    description: "Read the current Athena research phase, status, budgets, and SOTA experiment.",
    parameters: objectSchema({}, []),
    output: {
      schema: { type: "object" },
      render: (_args, value) => [textBlock(JSON.stringify(value))],
      presentationMeta: (_args, value) => value,
    },
    presentCall: (): ToolCallView => ({
      card: "generic",
      kind: "search",
      title: "查看 Athena 研究状态",
    }),
    presentResult: (_args, result): ToolResultView => ({
      card: "generic",
      title: "Athena 研究状态",
      content: result.meta === undefined ? result.content : statusBlocks(result.meta),
    }),
    async execute() {
      return researchStatus(ctx)
    },
  }
}

function treeTool(ctx: Context): ToolDefinition {
  return {
    name: "research_tree",
    description: "Read the Athena research tree: SOTA, experiments, and pending hypotheses.",
    parameters: objectSchema({}, []),
    output: {
      schema: { type: "object" },
      render: (_args, value) => [textBlock(JSON.stringify(value))],
      presentationMeta: (_args, value) => value,
    },
    presentCall: (): ToolCallView => ({
      card: "generic",
      kind: "read",
      title: "查看 Athena 研究树",
    }),
    presentResult: (_args, result): ToolResultView => ({
      card: "generic",
      title: "Athena 研究树",
      content: result.meta === undefined ? result.content : statusBlocks(result.meta),
    }),
    async execute() {
      return researchTreeSnapshot(ctx)
    },
  }
}

function runTool(ctx: Context, deps: RunToolDeps): ToolDefinition {
  return {
    name: "research_run",
    description: "Start the fixed research flow (PREPARE → SEARCH → VALIDATE).",
    parameters: objectSchema(
      {
        task: {
          type: "string",
          description: "Research task to execute. Defaults to a generic Athena research run.",
        },
      },
      []
    ),
    output: {
      schema: { type: "object" },
      render: (_args, value) => [textBlock(JSON.stringify(value))],
      presentationMeta: (_args, value) => value,
    },
    presentCall: (args): ToolCallView => ({
      card: "generic",
      kind: "execute",
      title: "运行 Athena 研究流程",
      rawInput: readTask(args),
    }),
    presentResult: (_args, result): ToolResultView => ({
      card: "generic",
      title: "Athena 研究流程已启动",
      content: result.meta === undefined ? result.content : statusBlocks(result.meta),
    }),
    async execute(args, exec: ToolRunContext) {
      const task = readTask(args)
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
  if (typeof args === "object" && args !== null && "task" in args) {
    const task = (args as { task: unknown }).task
    if (typeof task === "string" && task.trim() !== "") return task
  }
  return "Run the Athena research workflow (PREPARE → SEARCH → VALIDATE)."
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

  const planDecisionSchema = {
    type: "object",
    properties: {
      decision: { type: "string", enum: ["continue", "submit", "abandon"] },
      reason: { type: "string" },
      suggestions: { type: "array", items: { type: "string" } },
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
      const prompt = `Continue Plan ${planId}. Turns used: ${state.turns_used}; turn limit: ${state.turn_limit ?? "unlimited"}. Return JSON only.`
      return spawnStructured<PlanDecision>(prompt, planDecisionSchema)
    },
    runIdeatorTurn: async (count) => {
      const prompt = `Propose exactly ${count} falsifiable hypotheses to improve the primary metric. Return JSON only.`
      const batch = await spawnStructured<{ hypotheses: Array<Record<string, unknown>> }>(
        prompt,
        hypothesisBatchSchema
      )
      const parsed = HypothesisSchema.array().safeParse(batch?.hypotheses ?? [])
      return (parsed.success ? parsed.data : []) as Hypothesis[]
    },
    runPreparePhase: async () => {
      const agent = planDecisionAgent()
      const evaluatorDir = join(opts.projectRoot, "workspaces", "evaluator")
      mkdirSync(evaluatorDir, { recursive: true })
      const evaluatorRef = await runEvaluatorPlan({
        agent,
        scripts: ctx.researchScriptRunner,
        store: ctx.researchStore,
        evaluatorDir,
        task: opts.task,
        maxTurns: 20,
      })
      const baseCommit = await ctx.researchGit.init()
      const workspace = await ctx.researchGit.create(baseCommit, "athena/prepare")
      const treeRef = await ctx.researchStore.putText(JSON.stringify(ctx.researchTree.toDict()))
      return runPreparePlan({
        agent,
        evaluator: ctx.researchEvaluator,
        git: ctx.researchGit,
        workspace,
        execution: opts.execution,
        store: ctx.researchStore,
        evaluatorRef,
        treeRef,
        task: opts.task,
        maxTurns: 20,
      })
    },
    runValidationPhase: async (_commit, metric) => {
      const agent = planDecisionAgent()
      return runValidationPlan({
        agent,
        testScore: metric,
        direction: opts.direction,
        maxTurns: 20,
        runFinalTest: async () => metric, // TODO: 接 final-test PlanRunner 后替换
      })
    },
  }
}
