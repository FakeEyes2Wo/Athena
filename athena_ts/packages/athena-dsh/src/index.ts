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
import { Context, Service } from "@deepseek-ai/cordis"
import { LocalArtifactStore, LocalGitWorkspace, ResearchTree, type ArtifactRef } from "@athena/core"
import {
  DataScriptRunner,
  FixedFlowSupervisor,
  LocalExecutionRuntime,
  PlanRunner,
  Recovery,
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
import type { ToolDefinition } from "@deepseek-ai/dsh-tools"
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

/** 注册 Athena 研究域 Services + 两个模型可见工具。 */
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
    const recovery = new Recovery()
    const supervisor = new FixedFlowSupervisor({
      projectRoot,
      state,
      tree,
      store,
      git,
      scheduler,
      recovery,
      evaluatorRef: baselineEvaluatorRef(tree),
      direction,
      tolerance,
      workers: makeWorkers(),
    })

    ctx.provide("researchStore", store)
    ctx.provide("researchTree", tree)
    ctx.provide("researchState", state)
    ctx.provide("researchScheduler", scheduler)
    ctx.provide("researchEvaluator", evaluator)
    ctx.provide("researchScriptRunner", scripts)
    ctx.provide("researchGit", git)
    ctx.provide("researchSupervisor", supervisor)

    // 模型可见工具：research/status（只读）+ research/run（启动固定流程）。
    ctx.tools.register(statusTool(ctx))
    ctx.tools.register(runTool(ctx))

    function makeWorkers(): SupervisorWorkers {
      return {
        publish: async () => {},
        runPlanAgentTurn: async () => null, // DSH subagent wiring pending
        runIdeatorTurn: async () => [], // DSH subagent wiring pending
        runPreparePhase: async () => {
          throw new Error("PREPARE LLM worker requires DSH subagent wiring (pending)")
        },
        runValidationPhase: async () => {
          throw new Error("VALIDATE LLM worker requires DSH subagent wiring (pending)")
        },
        runPlanTurn: async (planId, planState) => {
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
        },
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
      render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }],
    },
    async execute() {
      return {
        phase: ctx.researchState.phase,
        status: ctx.researchState.status,
        search_limit: ctx.researchState.search_limit,
        concurrency: ctx.researchState.concurrency,
        manual_mode: ctx.researchState.manual_mode,
        sota_experiment_id: ctx.researchTree.bestExperimentId(),
      }
    },
  }
}

function runTool(ctx: Context): ToolDefinition {
  return {
    name: "research_run",
    description: "Start the fixed research flow (PREPARE → SEARCH → VALIDATE).",
    parameters: objectSchema({}, []),
    output: {
      schema: { type: "object" },
      render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }],
    },
    async execute() {
      await ctx.researchSupervisor.start()
      return {
        phase: ctx.researchState.phase,
        status: ctx.researchState.status,
        sota_experiment_id: ctx.researchTree.bestExperimentId(),
      }
    },
  }
}

/** 默认导出：DSH 组合 ``name: '@athena/dsh'`` 按此加载插件。 */
export default researchPlugin

/**
 * 用 DSH subagent 接线 LLM worker（替代占位）。
 *
 * ``parent`` 与 ``signal`` 来自驱动工具的 ``exec.agent``/``exec.signal``——subagent
 * ``start`` 强制要求 ``parent: Agent``，所以 worker 不能脱离一次 agent 驱动的工具调用
 * 独立后台运行。研究流程由某个 agent 调用 ``research_run`` 工具驱动：该工具的
 * ``execute(args, exec)`` 用 ``exec.agent``/``exec.signal`` 调本工厂，再把返回的
 * ``SupervisorWorkers`` 交给 supervisor（需给 ``FixedFlowSupervisor`` 加一个
 * ``setWorkers`` 或每次 run 重建 supervisor）。
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
    const provider = ctx.subagents.list()[0] ?? "spawn"
    const run = await ctx.subagents.start(provider, {
      prompt: [{ type: "text", text: prompt }] as ContentBlock[],
      parent,
      signal,
      outputSchema: outputSchema as never,
    })
    try {
      const result = await run.result
      if (result.stopReason !== "completed") return null
      return (result.structured ?? null) as T | null
    } finally {
      await run.dispose()
    }
  }

  function planDecisionAgent(): AgentTurn {
    return {
      turn: (prompt) =>
        spawnStructured<PlanDecision>(prompt, planDecisionSchema),
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
      return (batch?.hypotheses ?? []) as never
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

// 保留 Service 导入引用，避免未使用告警（后续 worker 接线用 subagent 时移除）。
void Service
