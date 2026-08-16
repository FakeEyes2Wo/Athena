/**
 * 研究 Plan 的持久化合同与共享执行助手（移植 ``research/supervisor/plans.py`` 的
 * 数据模型部分；``forward_run_events``/``wait_run_events`` 依赖 AgentRuntime，延后 M4）。
 */

import { z } from "zod"
import { CommitHash, FiniteFloat, HypothesisSchema, LooseFloat, digestFromRef } from "@athena/core"

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

/** Hypothesis 的严格只读快照（等价 _FrozenHypothesis：extra=forbid + strict）。 */
const FrozenHypothesisSchema = HypothesisSchema.strict()

export type PlanKind = "PREPARE" | "SEARCH" | "VALIDATE"

const planStateBase = z.strictObject({
  kind: z.enum(["PREPARE", "SEARCH", "VALIDATE"]),
  context_ref: TrustedRef,
  turns_used: z.number().int().min(0),
  turn_limit: z.number().int().min(0).nullable(),
  patience: z.number().int().min(0).nullable().optional(),
  stale_rounds: z.number().int().min(0).optional(),
  best_ref: TrustedRef.nullable().optional(),
})

/** 一个未了结 Plan 的持久化状态（等价 PlanState）。 */
export const PlanStateSchema = planStateBase.superRefine((plan, ctx) => {
  if (plan.kind === "SEARCH") {
    if (plan.patience === undefined || plan.patience === null) {
      ctx.addIssue({ code: "custom", message: "SEARCH Plan requires patience" })
    }
  } else {
    if (plan.patience !== undefined) {
      ctx.addIssue({
        code: "custom",
        message: "patience, stale_rounds and best_ref are SEARCH-only",
      })
    }
    if (plan.stale_rounds !== undefined) {
      ctx.addIssue({
        code: "custom",
        message: "patience, stale_rounds and best_ref are SEARCH-only",
      })
    }
    if (plan.best_ref !== undefined) {
      ctx.addIssue({
        code: "custom",
        message: "patience, stale_rounds and best_ref are SEARCH-only",
      })
    }
  }
})

export type PlanState = z.infer<typeof PlanStateSchema>

/** 序列化 PlanState：非 SEARCH 省略 search-only 字段（等价 model_serializer）。 */
export function planStateToJSON(plan: PlanState): Record<string, unknown> {
  if (plan.kind === "SEARCH") {
    return {
      kind: plan.kind,
      context_ref: plan.context_ref,
      turns_used: plan.turns_used,
      turn_limit: plan.turn_limit,
      patience: plan.patience ?? null,
      stale_rounds: plan.stale_rounds ?? 0,
      best_ref: plan.best_ref ?? null,
    }
  }
  return {
    kind: plan.kind,
    context_ref: plan.context_ref,
    turns_used: plan.turns_used,
    turn_limit: plan.turn_limit,
  }
}

/** Plan 随附的冻结输入产物（等价 PlanInput）。 */
export const PlanInputSchema = z.strictObject({
  hypothesis: FrozenHypothesisSchema.nullable().default(null),
  active_ancestor_hypotheses: z.array(FrozenHypothesisSchema).default([]),
  reference_experiment_id: z.string().nullable().default(null),
  reference_metric: LooseFloat.nullable().default(null),
  reference_priority: LooseFloat.default(0.0),
  direction: z.enum(["maximize", "minimize"]).default("maximize"),
  tolerance: z.number().min(0).default(0.0),
  evaluator_ref: TrustedRef,
  tree_ref: TrustedRef,
  human_context: z.string().default(""),
  initial_turn_limit: z.number().int().min(0).nullable().default(null),
  initial_patience: z.number().int().min(0).nullable().default(null),
})

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
  suggestions: z.array(z.string()).default([]),
})

export type PlanDecision = z.infer<typeof PlanDecisionSchema>
