/**
 * Athena 研究流程的 Artifact payload 合同（移植 ``research/contracts.py``）。
 */

import { z } from "zod"
import { ArtifactRef, CommitHash, FiniteFloat, HypothesisSchema, LooseFloat, NonBlankText, digestFromRef } from "@athena/core"

/** 冻结的 LLM 生成数据脚本 Bundle（python-uv）。 */
export const DataScriptBundleSchema = z.object({
  bundle_id: NonBlankText,
  entrypoint: z.string(),
  runtime: z.literal("python-uv").default("python-uv"),
  lock_ref: ArtifactRef.nullable().default(null),
  project_ref: ArtifactRef.nullable().default(null),
  source_ref: ArtifactRef.nullable().default(null),
  tree_ref: ArtifactRef.nullable().default(null),
  python_version: z.string().nullable().default(null),
  environment_hash: z.string().nullable().default(null),
})
export type DataScriptBundle = z.infer<typeof DataScriptBundleSchema>

/** VALIDATE 的最终结论（gap 超 tolerance 时 warning=true，仍 COMPLETED）。 */
export const ValidationResultSchema = z.object({
  result_id: NonBlankText,
  status: z.enum(["COMPLETED", "FAILED"]),
  test_score: LooseFloat.nullable().default(null),
  final_test_score: LooseFloat.nullable().default(null),
  generalization_gap: LooseFloat.nullable().default(null),
  generalization_warning: z.boolean().default(false),
})
export type ValidationResult = z.infer<typeof ValidationResultSchema>

/** 校验为 ``sha256:<64 hex>`` 的受信任内容引用。 */
const TrustedRef = z.string().refine(
  (value) => {
    try {
      digestFromRef(value)
      return true
    } catch {
      return false
    }
  },
  "Invalid artifact reference"
)

export const PlanStateSchema = z.strictObject({
  kind: z.enum(["PREPARE", "SEARCH", "VALIDATE"]),
  context_ref: TrustedRef,
  turns_used: z.number().int().min(0),
  turn_limit: z.number().int().min(0).nullable(),
  patience: z.number().int().min(0).nullable().optional(),
  stale_rounds: z.number().int().min(0).optional(),
  best_ref: TrustedRef.nullable().optional(),
}).superRefine((plan, ctx) => {
  if (plan.kind === "SEARCH") {
    if (plan.patience === undefined || plan.patience === null) {
      ctx.addIssue({ code: "custom", message: "SEARCH Plan requires patience" })
    }
  } else {
    for (const field of ["patience", "stale_rounds", "best_ref"] as const) {
      if (plan[field] !== undefined) {
        ctx.addIssue({ code: "custom", message: "patience, stale_rounds and best_ref are SEARCH-only" })
      }
    }
  }
})

export type PlanState = z.infer<typeof PlanStateSchema>

/** Plan 随附的冻结输入产物（等价 PlanInput）。 */
export const PlanInputSchema = z.preprocess((data) => {
  // Read old content-addressed Plan inputs without rewriting their artifact refs.
  if (typeof data !== "object" || data === null || Array.isArray(data)) return data
  const { active_ancestor_hypotheses, initial_turn_limit, initial_patience, ...input } =
    data as Record<string, unknown>
  return input
}, z.strictObject({
  hypothesis: HypothesisSchema.strict().nullable().default(null),
  reference_experiment_id: z.string().nullable().default(null),
  reference_metric: LooseFloat.nullable().default(null),
  reference_priority: LooseFloat.default(0.0),
  direction: z.enum(["maximize", "minimize"]).default("maximize"),
  tolerance: z.number().min(0).default(0.0),
  evaluator_ref: TrustedRef,
  tree_ref: TrustedRef,
  human_context: z.string().default(""),
}))

export type PlanInput = z.infer<typeof PlanInputSchema>

/** Plan 的不可变最佳可信分数记录（等价 PlanBest）。 */
export const PlanBestSchema = z.strictObject({
  metric: FiniteFloat,
  commit: CommitHash,
  evidence_ref: TrustedRef,
})

export type PlanBest = z.infer<typeof PlanBestSchema>

/** PlanAgent 每 turn 返回的结构化决定（等价 PlanDecision）。 */
export const PlanDecisionSchema = z.strictObject({
  decision: z.enum(["continue", "submit", "abandon"]),
  reason: z.string(),
})

export type PlanDecision = z.infer<typeof PlanDecisionSchema>
