/**
 * ResearchRuntime — 组合根（移植 ``research/runtime.py``，用 WorkerRunner 替代 AgentRuntime）。
 */

import { appendFileSync, existsSync, mkdirSync } from "node:fs"
import { isAbsolute, join, resolve } from "node:path"
import { ToolRegistry } from "@athena/agent"
import {
  LocalArtifactStore,
  LocalGitWorkspace,
  ResearchTree,
  type ArtifactRef,
  type Hypothesis,
} from "@athena/core"

import { DataScriptRunner } from "./script_runner.js"
import { TrustedEvaluator } from "./evaluation.js"
import { ExecutionContext, LocalExecutionRuntime } from "./execution.js"
import { PlanRunner } from "./supervisor/experiment.js"
import { Scheduler } from "./supervisor/scheduler.js"
import { ResearchState } from "./supervisor/state.js"
import { FixedFlowSupervisor, type SupervisorWorkers } from "./supervisor/supervisor.js"
import { runEvaluatorPlan, runPreparePlan, type AgentTurn } from "./supervisor/prepare.js"
import { runValidationPlan } from "./supervisor/validation.js"
import { WorkerRunner, HypothesisBatchOutputType, PlanDecisionOutputType } from "./worker.js"
import { makeShellTool } from "./shell.js"

const SETTINGS_WHITELIST = new Set([
  "concurrency",
  "search_limit",
  "ideator_count",
  "hypotheses_per_ideator",
  "manual_mode",
  "direction",
  "tolerance",
])

function maskSecret(value: string | undefined): string {
  if (!value) return ""
  return value.length <= 4 ? "****" : `${value.slice(0, 4)}****`
}

function isRelativeTo(candidate: string, root: string): boolean {
  const r = resolve(root)
  const c = resolve(candidate)
  return c === r || c.startsWith(r + "\\") || c.startsWith(r + "/")
}

export interface ResearchRuntimeOptions {
  projectRoot?: string
  stateRoot?: string
  model?: string | null
  client?: unknown
  task?: string
  searchLimit?: number
  concurrency?: number
  ideatorCount?: number
  hypothesesPerIdeator?: number
  direction?: "maximize" | "minimize"
  tolerance?: number
}

export class ResearchRuntime {
  readonly root: string
  readonly state: ResearchState
  readonly tree: ResearchTree
  readonly supervisor: FixedFlowSupervisor
  private store: LocalArtifactStore
  private git: LocalGitWorkspace
  private execution: LocalExecutionRuntime
  private evaluator: TrustedEvaluator
  private scripts: DataScriptRunner
  private worker: WorkerRunner | null
  private model: string | null
  private client: unknown
  private direction: "maximize" | "minimize"
  private tolerance: number
  private taskText: string
  private athenaRoot: string
  private workspacesRoot: string
  private statePath: string
  private treePath: string
  private sessionLogPath: string
  private agentLogDir: string

  constructor(opts: ResearchRuntimeOptions = {}) {
    this.root = resolve(opts.projectRoot ?? ".")
    this.athenaRoot = resolve(opts.stateRoot ?? join(this.root, ".athena"))
    this.workspacesRoot =
      opts.stateRoot !== undefined
        ? join(this.athenaRoot, "workspaces")
        : join(this.root, "workspaces")
    this.statePath = join(this.athenaRoot, "state.json")
    this.treePath = join(this.athenaRoot, "research_tree.json")
    this.sessionLogPath = join(this.athenaRoot, "logs", "sessions")
    this.agentLogDir = join(this.athenaRoot, "logs", "agents")
    mkdirSync(this.sessionLogPath, { recursive: true })
    mkdirSync(this.agentLogDir, { recursive: true })
    this.store = new LocalArtifactStore(join(this.athenaRoot, "artifacts"))
    this.git = new LocalGitWorkspace(join(this.athenaRoot, "repo"), this.workspacesRoot, (b) => this.store.putBytes(b))
    this.execution = new LocalExecutionRuntime(this.root, this.root)
    this.scripts = new DataScriptRunner(this.store, join(this.athenaRoot, "runs"))
    this.evaluator = new TrustedEvaluator(this.scripts)
    this.model = opts.model ?? null
    this.client = opts.client ?? null
    this.direction = opts.direction ?? "maximize"
    this.tolerance = opts.tolerance ?? 0.0
    this.taskText = opts.task ?? ""
    this.tree = existsSync(this.treePath) ? ResearchTree.load(this.treePath) : new ResearchTree()
    this.state = existsSync(this.statePath)
      ? ResearchState.load(this.statePath)
      : new ResearchState({
          status: "RUNNING",
          phase: "PREPARE",
          search_limit: opts.searchLimit ?? 10,
          concurrency: opts.concurrency ?? 4,
          ideator_count: opts.ideatorCount ?? 3,
          hypotheses_per_ideator: opts.hypothesesPerIdeator ?? 2,
        })
    // 断点续传保护：跨目录拷贝来的 state 会携带旧项目的 eda_dir。强制校验其
    // 属于当前 project_root，否则置空让 PREPARE 按本项目重建。
    if (this.state.eda_dir !== null) {
      const edaPath = isAbsolute(this.state.eda_dir)
        ? this.state.eda_dir
        : join(this.root, this.state.eda_dir)
      if (!isRelativeTo(edaPath, this.root)) {
        this.state.eda_dir = null
      }
    }
    this.worker = this.model ? new WorkerRunner({ store: this.store, model: this.model, client: this.client }) : null

    this.supervisor = new FixedFlowSupervisor({
      projectRoot: this.root,
      state: this.state,
      tree: this.tree,
      store: this.store,
      git: this.git,
      scheduler: new Scheduler(),
      evaluatorRef: this.baselineEvaluatorRef(),
      direction: this.direction,
      tolerance: this.tolerance,
      workers: this.makeWorkers(),
    })
  }

  private baselineEvaluatorRef(): ArtifactRef | null {
    const baselines = this.tree.experiments("baseline")
    return baselines.length > 0 ? baselines[0]!.plan.run_config_ref : null
  }

  private makeAgentTurn(): AgentTurn {
    const rt = this
    return {
      turn: async (prompt) => {
        if (!rt.worker) return null
        const tools = new ToolRegistry()
        tools.register(makeShellTool(rt.execution, new ExecutionContext(rt.root, rt.root, rt.root)))
        return rt.worker.runStructured<never>(prompt, PlanDecisionOutputType as never, { tools }) as never
      },
    }
  }

  private makeWorkers(): SupervisorWorkers {
    const rt = this
    return {
      publish: async () => {},
      runPlanAgentTurn: async (planId, state) => {
        if (!rt.worker) return null
        const prompt = `Continue Plan ${planId}. Turns used: ${state.turns_used}; turn limit: ${state.turn_limit ?? "unlimited"}.`
        return rt.worker.planDecision(prompt)
      },
      runIdeatorTurn: async (count) => {
        if (!rt.worker) return []
        const batch = await rt.worker.runStructured<{ hypotheses: Hypothesis[] }>(
          `Propose exactly ${count} falsifiable hypotheses to improve the primary metric.`,
          HypothesisBatchOutputType
        )
        return batch.hypotheses
      },
      runPlanTurn: async (planId, state) => {
        const planInput = await rt.supervisor.planInput(planId)
        const runner = new PlanRunner(
          rt.execution,
          rt.store,
          rt.evaluator,
          rt.git,
          rt.supervisor.workspace(planId),
          { project_root: rt.root, workspace_root: rt.supervisor.workspacePath(planId), environment_root: rt.root, experiment_id: planId },
          planInput.direction
        )
        return runner.runTurn(planId, state, planInput)
      },
      runPreparePhase: async () => {
        if (!rt.worker) throw new Error("PREPARE requires a registered Agent provider")
        const agent = rt.makeAgentTurn()
        const evaluatorDir = join(rt.workspacesRoot, "evaluator")
        mkdirSync(evaluatorDir, { recursive: true })
        const evaluatorRef = await runEvaluatorPlan({
          agent,
          scripts: rt.scripts,
          store: rt.store,
          evaluatorDir,
          task: rt.taskText,
          maxTurns: 20,
        })
        const baseCommit = await rt.git.init(undefined, ".gitignore", ".venv/\n")
        const workspace = await rt.git.create(baseCommit, "athena/prepare")
        const treeRef = await rt.store.putText(JSON.stringify(rt.tree.toDict()))
        return runPreparePlan({
          agent,
          evaluator: rt.evaluator,
          git: rt.git,
          workspace,
          execution: rt.execution,
          store: rt.store,
          evaluatorRef,
          treeRef,
          task: rt.taskText,
          maxTurns: 20,
        })
      },
      runValidationPhase: async (_commit, metric) => {
        if (!rt.worker) throw new Error("VALIDATE requires a registered Agent provider")
        return runValidationPlan({
          agent: rt.makeAgentTurn(),
          testScore: metric,
          direction: rt.direction,
          maxTurns: 20,
          runFinalTest: async () => metric,
        })
      },
    }
  }

  private appendSessionLog(kind: string, data: Record<string, unknown>): void {
    const line = JSON.stringify({
      at: new Date().toISOString(),
      kind,
      ...data,
    })
    appendFileSync(join(this.sessionLogPath, "runtime.jsonl"), `${line}\n`, "utf-8")
  }

  /** 将当前研究树持久化到 ``.athena/research_tree.json``。 */
  save_tree(): string {
    return this.tree.save(this.treePath)
  }

  /** 从磁盘重新加载研究树（只读快照）。 */
  load_tree(): ResearchTree {
    return ResearchTree.load(this.treePath)
  }

  settings(): Record<string, unknown> {
    return {
      project_root: this.root,
      model: this.model,
      concurrency: this.state.concurrency,
      search_limit: this.state.search_limit,
      ideator_count: this.state.ideator_count,
      hypotheses_per_ideator: this.state.hypotheses_per_ideator,
      manual_mode: this.state.manual_mode,
      direction: this.direction,
      tolerance: this.tolerance,
      phase: this.state.phase,
      status: this.state.status,
      task_understanding: this.state.task_understanding,
      model_connection: {
        provider: process.env.LLM_PROVIDER ?? "deepseek",
        base_url: process.env.BASE_URL ?? "",
        model_name: process.env.MODEL_NAME ?? "",
        llm_api_key: maskSecret(
          process.env.LLM_API_KEY ?? process.env.DEEPSEEK_API_KEY ?? process.env.OPENAI_API_KEY,
        ),
      },
    }
  }

  /** 应用白名单内的运行设置；持久字段会写回 ``.athena/state.json``。 */
  async applySettings(patch: Record<string, unknown>): Promise<Record<string, unknown>> {
    const unknown = Object.keys(patch).filter((key) => !SETTINGS_WHITELIST.has(key))
    if (unknown.length > 0) {
      throw new Error(`unsupported settings fields: ${[...unknown].sort().join(", ")}`)
    }

    if ("concurrency" in patch) {
      const value = patch.concurrency
      if (typeof value !== "number" || !Number.isInteger(value) || value < 1) {
        throw new Error("concurrency must be an integer >= 1")
      }
      this.state.concurrency = value
    }
    if ("search_limit" in patch) {
      const value = patch.search_limit
      if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
        throw new Error("search_limit must be an integer >= 0")
      }
      this.state.search_limit = value
    }
    if ("ideator_count" in patch) {
      const value = patch.ideator_count
      if (typeof value !== "number" || !Number.isInteger(value) || value < 1 || value > 8) {
        throw new Error("ideator_count must be an integer between 1 and 8")
      }
      this.state.ideator_count = value
    }
    if ("hypotheses_per_ideator" in patch) {
      const value = patch.hypotheses_per_ideator
      if (typeof value !== "number" || !Number.isInteger(value) || value < 1 || value > 5) {
        throw new Error("hypotheses_per_ideator must be an integer between 1 and 5")
      }
      this.state.hypotheses_per_ideator = value
    }
    if ("manual_mode" in patch) {
      const value = patch.manual_mode
      if (typeof value !== "boolean") {
        throw new Error("manual_mode must be a bool")
      }
      this.state.manual_mode = value
    }
    if ("direction" in patch) {
      const value = patch.direction
      if (value !== "maximize" && value !== "minimize") {
        throw new Error("direction must be 'maximize' or 'minimize'")
      }
      this.direction = value
    }
    if ("tolerance" in patch) {
      const value = patch.tolerance
      if (typeof value !== "number" || value < 0) {
        throw new Error("tolerance must be a number >= 0")
      }
      this.tolerance = value
    }

    if (
      "concurrency" in patch ||
      "search_limit" in patch ||
      "ideator_count" in patch ||
      "hypotheses_per_ideator" in patch ||
      "manual_mode" in patch
    ) {
      this.state.save(this.statePath)
    }
    return this.settings()
  }

  async start(): Promise<void> {
    await this.git.init(undefined, ".gitignore", ".venv/\n")
    this.appendSessionLog("runtime.started", { phase: this.state.phase })
    await this.supervisor.start()
  }

  async message(text: string): Promise<string> {
    this.appendSessionLog("runtime.message", { text })
    const command = text.trim()
    if (command === "/stop") return this.supervisor.requestStop()
    if (command === "/pause") return this.supervisor.pause()
    if (command === "/resume") return this.supervisor.resume()
    return this.supervisor.requestStop()
  }

  async aclose(): Promise<void> {
    this.appendSessionLog("runtime.stopped", { phase: this.state.phase })
    await this.supervisor.stop()
  }
}
