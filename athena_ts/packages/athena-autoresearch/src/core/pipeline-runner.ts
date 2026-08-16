import type { StageSpec } from "../schemas/run-spec.js"
import type { AutoResearchState } from "../schemas/state.js"
import { AutoResearchStateStore } from "../schemas/state.js"
import type { GateResult, GateRunner } from "./gate-runner.js"
import type { Stage, StageContext, StageResult } from "./pipeline-types.js"

export interface RunResult {
  status: AutoResearchState["status"]
  gateResults: GateResult[]
}

export class PipelineRunner {
  constructor(
    private readonly stages: ReadonlyMap<string, Stage>,
    private readonly stateStore: AutoResearchStateStore,
    private readonly gateRunner: GateRunner,
  ) {}

  async run(ctx: StageContext): Promise<RunResult> {
    const spec = ctx.spec
    const done = new Set<string>()
    const results: GateResult[] = []

    while (ctx.state.status === "RUNNING") {
      if (ctx.budgets.timeLimitReached()) {
        ctx.state = this.stateStore.setStatus(ctx.state, "WAITING", "TIME_LIMIT_REACHED")
        break
      }

      const next = this.findNext(spec.stages, done, ctx)
      if (!next) {
        ctx.state = this.stateStore.setStatus(
          ctx.state,
          done.size === spec.stages.length ? "COMPLETED" : "WAITING",
          done.size === spec.stages.length ? "ALL_STAGES_DONE" : "NO_ENTERABLE_STAGE",
        )
        break
      }

      const stage = this.stages.get(next.provider)
      if (!stage) {
        throw new Error(`unknown stage provider: ${next.provider}`)
      }

      ctx.console.info(`[autoresearch] stage_started ${next.id} (provider=${next.provider})`)
      const result = await this.runWithRetry(stage, next, ctx)
      done.add(next.id)

      if (result.status === "FAILED") {
        ctx.state = this.stateStore.setStatus(ctx.state, "FAILED", next.id)
        results.push(...(result.gateResults ?? []))
        break
      }
      if (result.status === "WAITING") {
        ctx.state = this.stateStore.setStatus(ctx.state, "WAITING", next.id)
        results.push(...(result.gateResults ?? []))
        break
      }

      ctx.state = this.stateStore.setPhase(ctx.state, next.id)

      const gateRefs = next.gates ?? []
      const gateResults = await this.gateRunner.run(gateRefs, ctx, result)
      results.push(...gateResults)
      ctx.state = this.stateStore.setGateResults(
        ctx.state,
        gateResults.map((gate) => ({ ...gate } as Record<string, unknown>)),
      )

      if (gateResults.some((gate) => !gate.pass)) {
        ctx.state = this.stateStore.setStatus(ctx.state, "WAITING", "GATE_FAILED")
        break
      }

      if (result.nextStage) {
        ctx.state = this.stateStore.setPhase(ctx.state, result.nextStage)
      }
      ctx.console.info(`[autoresearch] stage_completed ${next.id}`)
    }

    return { status: ctx.state.status, gateResults: results }
  }

  private findNext(stages: readonly StageSpec[], done: ReadonlySet<string>, ctx: StageContext): StageSpec | undefined {
    return stages.find(
      (stageSpec) =>
        !done.has(stageSpec.id) &&
        (stageSpec.depends_on ?? []).every((dep) => done.has(dep)) &&
        this.canEnter(stageSpec, ctx),
    )
  }

  private canEnter(stageSpec: StageSpec, ctx: StageContext): boolean {
    const stage = this.stages.get(stageSpec.provider)
    if (!stage) return false
    return stage.canEnter(ctx)
  }

  private async runWithRetry(stage: Stage, spec: StageSpec, ctx: StageContext): Promise<StageResult> {
    const maxAttempts = 1 + (spec.retry?.max ?? 0)
    const backoffMs = spec.retry?.backoff_ms ?? 0
    let last: StageResult = { status: "FAILED" }

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      last = await stage.run(ctx)
      if (last.status !== "FAILED" || attempt === maxAttempts) return last
      if (backoffMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, backoffMs))
      }
    }
    return last
  }
}
