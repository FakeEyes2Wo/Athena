import { existsSync, readFileSync } from "node:fs"
import { atomicWriteJson, parseOrThrow, type ResearchTree } from "@athena/core"
import type { PoolStatus, PooledHypothesis } from "../schemas/pool.js"
import { HypothesisPoolFileSchema, PooledHypothesisSchema } from "../schemas/pool.js"
import type { HypothesisPoolLike } from "./pipeline-types.js"

export interface SettlePoolInput {
  poolId: string
  outcome: "WIN" | "DRAW" | "LOSS" | "INCONCLUSIVE"
  metric?: number | null
  referenceMetric?: number | null
  direction?: "maximize" | "minimize"
  experimentId?: string
  sotaAfter?: string | null
}

export interface PromotePoolInput {
  poolId: string
  paperRefs: string[]
  sectionIds: string[]
  negativeResult: boolean
}

export class HypothesisPool implements HypothesisPoolLike {
  private entries = new Map<string, PooledHypothesis>()

  constructor(private readonly path: string) {}

  load(): void {
    if (!existsSync(this.path)) {
      this.entries.clear()
      return
    }
    const file = parseOrThrow(HypothesisPoolFileSchema, JSON.parse(readFileSync(this.path, "utf-8")))
    this.entries = new Map(Object.entries(file.entries))
  }

  save(): string {
    const file: unknown = {
      version: 1,
      entries: Object.fromEntries(this.entries),
    }
    return atomicWriteJson(this.path, file)
  }

  has(poolId: string): boolean {
    return this.entries.has(poolId)
  }

  get(poolId: string): PooledHypothesis {
    const entry = this.entries.get(poolId)
    if (!entry) throw new Error(`unknown pool entry: ${poolId}`)
    return entry
  }

  upsert(entry: PooledHypothesis): void {
    const parsed = PooledHypothesisSchema.parse({ ...entry, updated_at: new Date().toISOString() })
    this.entries.set(parsed.pool_id, parsed)
  }

  list(status?: PoolStatus): PooledHypothesis[] {
    return [...this.entries.values()].filter((entry) => !status || entry.pool_status === status)
  }

  queued(): PooledHypothesis[] {
    return this.list("QUEUED")
  }

  supported(): PooledHypothesis[] {
    return this.list("SUPPORTED")
  }

  refuted(): PooledHypothesis[] {
    return this.list("REFUTED")
  }

  inconclusive(): PooledHypothesis[] {
    return this.list("INCONCLUSIVE")
  }

  promotedToPaper(): PooledHypothesis[] {
    return this.list("PROMOTED_TO_PAPER")
  }

  negativeResults(): PooledHypothesis[] {
    return this.list("NEGATIVE_RESULT")
  }

  countQueued(): number {
    return this.queued().length
  }

  countByOrigin(origin: string): number {
    return this.list().filter((entry) => entry.origin === origin).length
  }

  recordSettlement(input: SettlePoolInput): void {
    const entry = this.get(input.poolId)
    const outcome = input.outcome
    const poolStatus: PoolStatus =
      outcome === "WIN" ? "SUPPORTED" : outcome === "INCONCLUSIVE" ? "INCONCLUSIVE" : "REFUTED"
    const direction = input.direction ?? entry.metric_direction
    const referenceMetric = input.referenceMetric ?? null
    const metric = input.metric ?? null
    const delta =
      metric !== null && referenceMetric !== null
        ? direction === "maximize"
          ? metric - referenceMetric
          : referenceMetric - metric
        : null
    this.upsert({
      ...entry,
      pool_status: poolStatus,
      best_metric: metric ?? entry.best_metric,
      delta_vs_reference: delta,
      comparison_outcome: outcome === "INCONCLUSIVE" ? null : outcome,
      experiment_ids: input.experimentId
        ? [...new Set([...entry.experiment_ids, input.experimentId])]
        : entry.experiment_ids,
    })
  }

  recordGateRejection(poolId: string, gateSummary: Record<string, unknown>): void {
    const entry = this.get(poolId)
    this.upsert({ ...entry, pool_status: "REJECTED", gate_summary: gateSummary })
  }

  recordPaperPromotion(input: PromotePoolInput): void {
    const entry = this.get(input.poolId)
    this.upsert({
      ...entry,
      pool_status: input.negativeResult ? "NEGATIVE_RESULT" : "PROMOTED_TO_PAPER",
      paper_refs: [...new Set([...entry.paper_refs, ...input.paperRefs])],
      paper_section_ids: [...new Set([...entry.paper_section_ids, ...input.sectionIds])],
      negative_result: input.negativeResult,
    })
  }

  archive(poolId: string): void {
    const entry = this.get(poolId)
    this.upsert({ ...entry, pool_status: "ARCHIVED" })
  }

  supersede(poolId: string): void {
    const entry = this.get(poolId)
    this.upsert({ ...entry, pool_status: "SUPERSEDED" })
  }

  /** 从 ResearchTree 对账：树中有而池中没有的假设按树状态导入；池有树无的保留。 */
  reconcile(tree: ResearchTree): void {
    for (const hypothesis of Object.values(tree.toDict().hypotheses)) {
      const poolId = hypothesis.id
      if (poolId === null || this.entries.has(poolId)) continue
      const poolStatus: PoolStatus =
        hypothesis.status === "PROPOSED"
          ? "QUEUED"
          : hypothesis.status === "SUPPORTED"
            ? "SUPPORTED"
            : hypothesis.status === "REFUTED"
              ? "REFUTED"
              : "REJECTED"
      const now = new Date().toISOString()
      this.entries.set(
        poolId,
        PooledHypothesisSchema.parse({
          pool_id: poolId,
          hypothesis,
          pool_status: poolStatus,
          origin: "research_tree_import",
          created_at: now,
          updated_at: now,
        })
      )
    }
  }

  toJSON(): Record<string, unknown> {
    return { version: 1, entries: Object.fromEntries(this.entries) }
  }
}
