import type { ArtifactStore, ResearchTree } from "@athena/core"
import type { RunSpec } from "../schemas/run-spec.js"
import type { AutoResearchState } from "../schemas/state.js"
import type { BudgetService } from "./budget-service.js"
import type { ConsolePort } from "./console-port.js"
import type { ProviderRegistry } from "./provider-registry.js"
import type { GateResult } from "./gate-runner.js"

export interface StageContext {
  readonly runId: string
  readonly projectRoot: string
  readonly spec: RunSpec
  state: AutoResearchState
  readonly pool: HypothesisPoolLike
  readonly tree: ResearchTree
  readonly artifacts: ArtifactStore
  readonly providers: ProviderRegistry
  readonly budgets: BudgetService
  readonly console: ConsolePort
}

export interface HypothesisPoolLike {
  countQueued(): number
  countByOrigin(origin: string): number
}

export const emptyPool: HypothesisPoolLike = {
  countQueued: () => 0,
  countByOrigin: () => 0,
}

export interface Stage {
  readonly id: string
  canEnter(ctx: StageContext): boolean
  run(ctx: StageContext): Promise<StageResult>
}

export type StageStatus = "COMPLETED" | "WAITING" | "FAILED" | "SKIPPED"

export interface StageResult {
  status: StageStatus
  nextStage?: string
  artifacts?: Record<string, string>
  gateResults?: GateResult[]
  consoleLines?: string[]
}
