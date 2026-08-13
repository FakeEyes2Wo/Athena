import { z } from "zod"
import { ArtifactRef, NonBlankText } from "./contracts.js"

export const HypothesisStatus = ["PROPOSED", "SUPPORTED", "REFUTED", "REJECTED"] as const
export type HypothesisStatus = (typeof HypothesisStatus)[number]

/** 可通过实验验证或证伪的机器学习假设。 */
export const HypothesisSchema = z.object({
  statement: NonBlankText,
  intervention: NonBlankText,
  expected_effect: NonBlankText,
  status: z.enum(HypothesisStatus).default("PROPOSED"),
  evidence_refs: z.array(ArtifactRef).default([]),
  id: z.string().nullable().default(null),
  parent_id: z.string().nullable().default(null),
  supersedes: z.array(z.string()).default([]),
  priority: z.number().finite().default(1000.0),
  order: z.number().int().min(0).nullable().default(null),
  patience: z.number().int().min(0).default(0),
  turn_limit: z.number().int().min(0).nullable().default(null),
  sources: z.array(z.string()).default([]),
})
export type Hypothesis = z.infer<typeof HypothesisSchema>

/** 结构化 Ideator 输出：一组可验证假设。 */
export const HypothesisBatchSchema = z.object({
  hypotheses: z.array(HypothesisSchema).default([]),
})
export type HypothesisBatch = z.infer<typeof HypothesisBatchSchema>

/** 实验计划。 */
export const ExperimentPlanSchema = z.object({
  kind: z.string(),
  change: z.string(),
  rubrics: z.array(z.string()).default([]),
  run_config_ref: ArtifactRef,
  budget: z.record(z.string(), z.unknown()),
  acceptance_rule: z.string(),
})
export type ExperimentPlan = z.infer<typeof ExperimentPlanSchema>

/** 在预测上运行 eval 的输出。 */
export const EvalResultSchema = z.object({
  experiment_id: z.string(),
  primary: z.number(),
  secondary: z.record(z.string(), z.number()).default({}),
  per_sample: ArtifactRef,
})
export type EvalResult = z.infer<typeof EvalResultSchema>

/** 两个实验的两两比较。 */
export const ComparisonVerdictSchema = z.object({
  winner: z.enum(["baseline", "candidate", "tie"]),
  p_value: z.number(),
})
export type ComparisonVerdict = z.infer<typeof ComparisonVerdictSchema>
