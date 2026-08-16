import type { FixedFlowSupervisor } from "@athena/research"
import type { StageContext } from "../../core/pipeline-types.js"
import type { ExperimentEngineProvider, SettlementEvent } from "../types.js"

export interface AthenaEngineDeps {
  supervisor: FixedFlowSupervisor
}

/** 把 Athena 子集（@athena/research 的 FixedFlowSupervisor）适配为实验引擎。 */
export class AthenaExperimentEngine implements ExperimentEngineProvider {
  readonly id = "athena" as const
  readonly version = "0.1.0"
  readonly capabilities = ["experiment.engine", "experiment.athena"]

  constructor(private readonly deps: AthenaEngineDeps) {}

  async start(ctx: StageContext): Promise<void> {
    ctx.console.info(`[athena-engine] start (phase=${this.deps.supervisor.state.phase})`)
    await this.deps.supervisor.start()
    ctx.console.info(
      `[athena-engine] done (phase=${this.deps.supervisor.state.phase}, status=${this.deps.supervisor.state.status})`,
    )
  }

  async recover(ctx: StageContext): Promise<void> {
    ctx.console.info("[athena-engine] recover")
    await this.deps.supervisor.recover()
  }

  async *settleEvents(_ctx: StageContext): AsyncIterable<SettlementEvent> {
    // 首版：从 @athena/research 的 publish/state 流差量推导；当前占位为空。
    yield* []
  }
}
