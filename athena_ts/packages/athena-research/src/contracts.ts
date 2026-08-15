/**
 * Athena 研究流程的 Artifact payload 合同（移植 ``research/contracts.py``）。
 */

import { z } from "zod"
import { ArtifactRef, LooseFloat, NonBlankText } from "@athena/core"

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

/** 单个候选实验的评估结果（trusted evaluator 产出）。 */
export const CandidateEvaluationSchema = z.object({
  candidate_id: NonBlankText,
  test_score: LooseFloat,
  kfold_mean: LooseFloat.nullable().default(null),
  kfold_std: LooseFloat.nullable().default(null),
  direction: z.enum(["maximize", "minimize"]),
})
export type CandidateEvaluation = z.infer<typeof CandidateEvaluationSchema>

/** VALIDATE 的最终结论（gap 超 tolerance 时 warning=true，仍 COMPLETED）。 */
export const ValidationResultSchema = z.object({
  result_id: NonBlankText,
  status: z.enum(["COMPLETED", "FAILED"]),
  test_score: LooseFloat.nullable().default(null),
  final_test_score: LooseFloat.nullable().default(null),
  generalization_gap: LooseFloat.nullable().default(null),
  generalization_warning: z.boolean().default(false),
  sota_commit: z.string().nullable().default(null),
  validation_commit: z.string().nullable().default(null),
  predictions_ref: ArtifactRef.nullable().default(null),
  predictions_path: z.string().nullable().default(null),
  evidence_ref: ArtifactRef.nullable().default(null),
})
export type ValidationResult = z.infer<typeof ValidationResultSchema>
