import { join, resolve } from "node:path"
import { LocalArtifactStore, ResearchTree } from "@athena/core"
import {
  createDefaultGateRunner,
  createDefaultProviders,
  createDefaultStages,
} from "./bootstrap.js"
import { BudgetService } from "./core/budget-service.js"
import type { ConsolePort } from "./core/console-port.js"
import { MemoryConsolePort } from "./core/console-port.js"
import type { GateRunner } from "./core/gate-runner.js"
import { emptyPool, type HypothesisPoolLike, type Stage, type StageContext } from "./core/pipeline-types.js"
import { PipelineRunner, type RunResult } from "./core/pipeline-runner.js"
import type { ProviderRegistry } from "./core/provider-registry.js"
import { AutoResearchStateStore, type AutoResearchState } from "./schemas/state.js"
import { DEFAULT_RUN_SPEC, mergeRunSpecs, type RunSpec, type RunSpecOverride } from "./schemas/run-spec.js"

export interface AutoResearchRuntimeOptions {
  projectRoot?: string
  runSpec?: RunSpecOverride
  tree?: ResearchTree
  pool?: HypothesisPoolLike
  artifacts?: LocalArtifactStore
  providers?: ProviderRegistry
  stages?: Map<string, Stage>
  gateRunner?: GateRunner
  console?: ConsolePort
  stateStore?: AutoResearchStateStore
}

export class AutoResearchRuntime {
  private readonly projectRoot: string
  private readonly spec: RunSpec
  private readonly tree: ResearchTree
  private readonly pool: HypothesisPoolLike
  private readonly artifacts: LocalArtifactStore
  private readonly providers: ProviderRegistry
  private readonly stages: Map<string, Stage>
  private readonly gateRunner: GateRunner
  private readonly console: ConsolePort
  private readonly stateStore: AutoResearchStateStore
  private state: AutoResearchState
  private budgets: BudgetService

  constructor(opts: AutoResearchRuntimeOptions = {}) {
    this.projectRoot = resolve(opts.projectRoot ?? ".")
    this.spec = mergeRunSpecs(DEFAULT_RUN_SPEC, opts.runSpec ?? {})
    const athenaDir = join(this.projectRoot, ".athena", "autoresearch")
    this.stateStore = opts.stateStore ?? new AutoResearchStateStore(join(athenaDir, "state.json"))
    this.tree = opts.tree ?? new ResearchTree()
    this.pool = opts.pool ?? emptyPool
    this.artifacts = opts.artifacts ?? new LocalArtifactStore(join(athenaDir, "artifacts"))
    this.providers = opts.providers ?? createDefaultProviders()
    this.stages = opts.stages ?? createDefaultStages()
    this.gateRunner = opts.gateRunner ?? createDefaultGateRunner()
    this.console = opts.console ?? new MemoryConsolePort()
    this.budgets = new BudgetService(this.spec.budgets)
    this.state = this.stateStore.create({
      run_id: this.spec.run_id ?? `ar_${Date.now().toString(36)}`,
      phase: this.spec.stages[0]?.id ?? "intake",
      stage_id: this.spec.stages[0]?.id ?? "intake",
      status: "RUNNING",
      paper_spec: this.spec.paper_spec,
      budgets: { ...this.spec.budgets },
      started_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      deadline: new Date(this.budgets.deadline()).toISOString(),
    })
  }

  get currentState(): AutoResearchState {
    return this.state
  }

  get currentSpec(): RunSpec {
    return this.spec
  }

  getTree(): ResearchTree {
    return this.tree
  }

  getPool(): HypothesisPoolLike {
    return this.pool
  }

  async start(): Promise<RunResult> {
    this.state = this.stateStore.setStatus(this.state, "RUNNING")
    const runner = new PipelineRunner(this.stages, this.stateStore, this.gateRunner)
    const ctx = this.createContext()
    const result = await runner.run(ctx)
    this.state = ctx.state
    this.persistSideEffects()
    return result
  }

  async resume(): Promise<RunResult> {
    const runner = new PipelineRunner(this.stages, this.stateStore, this.gateRunner)
    const ctx = this.createContext()
    const result = await runner.run(ctx)
    this.state = ctx.state
    this.persistSideEffects()
    return result
  }

  private createContext(): StageContext {
    return {
      runId: this.state.run_id,
      projectRoot: this.projectRoot,
      spec: this.spec,
      state: this.state,
      pool: this.pool,
      tree: this.tree,
      artifacts: this.artifacts,
      providers: this.providers,
      budgets: this.budgets,
      console: this.console,
    }
  }

  private persistSideEffects(): void {
    this.stateStore.save(this.state)
    const pool = this.pool as HypothesisPoolLike & { save?: () => void }
    pool.save?.()
  }
}
