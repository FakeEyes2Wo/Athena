import { z } from "zod"
import { HypothesisSchema } from "@athena/core"

export const PoolStatus = [
  "QUEUED",
  "SELECTED",
  "RUNNING",
  "SUPPORTED",
  "REFUTED",
  "INCONCLUSIVE",
  "REJECTED",
  "PROMOTED_TO_PAPER",
  "NEGATIVE_RESULT",
  "SUPERSEDED",
  "ARCHIVED",
] as const
export type PoolStatus = (typeof PoolStatus)[number]

export const PooledHypothesisSchema = z.strictObject({
  pool_id: z.string().min(1),
  hypothesis: HypothesisSchema,
  pool_status: z.enum(PoolStatus),
  origin: z.string().default("live_eda_ideator"),
  generation_strategy: z.string().nullable().default(null),
  gate_summary: z.record(z.string(), z.unknown()).nullable().default(null),
  experiment_ids: z.array(z.string()).default([]),
  best_metric: z.number().nullable().default(null),
  metric_direction: z.enum(["maximize", "minimize"]).default("maximize"),
  delta_vs_reference: z.number().nullable().default(null),
  comparison_outcome: z.enum(["WIN", "DRAW", "LOSS"]).nullable().default(null),
  paper_refs: z.array(z.string()).default([]),
  paper_section_ids: z.array(z.string()).default([]),
  negative_result: z.boolean().default(false),
  created_at: z.string(),
  updated_at: z.string(),
})
export type PooledHypothesis = z.infer<typeof PooledHypothesisSchema>

export const HypothesisPoolFileSchema = z.strictObject({
  version: z.literal(1),
  entries: z.record(z.string(), PooledHypothesisSchema),
})
export type HypothesisPoolFile = z.infer<typeof HypothesisPoolFileSchema>
