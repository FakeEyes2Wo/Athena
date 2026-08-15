/**
 * ResearchRuntime — 组合根（移植 ``research/runtime.py``，用 WorkerRunner 替代 AgentRuntime）。
 */

import { existsSync, mkdirSync } from "node:fs"
import { join, resolve } from "node:path"
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
import { Recovery } from "./supervisor/recovery.js"
import { ResearchState } from "./supervisor/state.js"
import { FixedFlowSupervisor, type SupervisorWorkers } from "./supervisor/supervisor.js"
import { runEvaluatorPlan, runPreparePlan, type AgentTurn } from "./supervisor/prepare.js"
import { runValidationPlan } from "./supervisor/validation.js"
import { WorkerRunner, HypothesisBatchOutputType, PlanDecisionOutputType } from "./worker.js"
import { makeShellTool } from "./shell.js"

export interface ResearchRuntimeOptions {
  projectRoot?: string
  model?: string | null
  client?: unknown
  task?: string
  searchLimit?: number
  concurrency?: number
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
  private statePath: string
  private treePath: string

  constructor(opts: ResearchRuntimeOptions = {}) {
    this.root = resolve(opts.projectRoot ?? ".")
    const athena = join(this.root, ".athena")
    this.statePath = join(athena, "state.json")
    this.treePath = join(athena, "research_tree.json")
    this.store = new LocalArtifactStore(join(athena, "artifacts"))
    this.git = new LocalGitWorkspace(join(athena, "repo"), join(this.root, "workspaces"), (b) => this.store.putBytes(b))
    this.execution = new LocalExecutionRuntime(this.root, this.root)
    this.scripts = new DataScriptRunner(this.store, join(athena, "runs"))
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
        })
    this.worker = this.model ? new WorkerRunner({ store: this.store, model: this.model, client: this.client }) : null

    this.supervisor = new FixedFlowSupervisor({
      projectRoot: this.root,
      state: this.state,
      tree: this.tree,
      store: this.store,
      git: this.git,
      scheduler: new Scheduler(),
      recovery: new Recovery(),
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
        const evaluatorDir = join(rt.root, "workspaces", "evaluator")
        mkdirSync(evaluatorDir, { recursive: true })
        const evaluatorRef = await runEvaluatorPlan({
          agent,
          scripts: rt.scripts,
          store: rt.store,
          evaluatorDir,
          task: rt.taskText,
          maxTurns: 20,
        })
        const baseCommit = await rt.git.init()
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
      runValidationPhase: async (commit, metric) => {
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

  settings(): Record<string, unknown> {
    return {
      project_root: this.root,
      model: this.model,
      concurrency: this.state.concurrency,
      search_limit: this.state.search_limit,
      direction: this.direction,
      tolerance: this.tolerance,
      phase: this.state.phase,
      status: this.state.status,
    }
  }

  async start(): Promise<void> {
    await this.git.init()
    await this.supervisor.start()
  }

  async message(text: string): Promise<string> {
    const command = text.trim()
    if (command === "/stop") return this.supervisor.requestStop()
    if (command === "/pause") return this.supervisor.pause()
    if (command === "/resume") return this.supervisor.resume()
    return this.supervisor.requestStop()
  }

  async aclose(): Promise<void> {
    await this.supervisor.stop()
  }
}
