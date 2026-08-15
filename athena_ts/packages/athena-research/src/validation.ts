/**
 * ValidationService — VALIDATE 阶段的泛化差距计算（移植 ``research/validation.py``）。
 */

import { newId } from "@athena/core"
import { ValidationResultSchema, type ValidationResult } from "./contracts.js"

const TIE_REL_TOL = 1e-9
const TIE_ABS_TOL = 1e-12

function isClose(a: number, b: number, relTol: number, absTol: number): boolean {
  return Math.abs(a - b) <= Math.max(relTol * Math.max(Math.abs(a), Math.abs(b)), absTol)
}

/** 计算 test 与 final-test 的泛化差距（正值表示 final_test 更差）。 */
export function generalizationGap(
  testScore: number,
  finalTestScore: number,
  direction: string
): number {
  if (direction === "minimize") {
    return finalTestScore - testScore
  }
  return testScore - finalTestScore
}

/** gap 为正且超过数值 tie tolerance 时 warning=true。 */
export function generalizationWarning(
  gap: number,
  opts: { relTol?: number; absTol?: number } = {}
): boolean {
  const relTol = opts.relTol ?? TIE_REL_TOL
  const absTol = opts.absTol ?? TIE_ABS_TOL
  return gap > 0 && !isClose(gap, 0.0, relTol, absTol)
}

export class ValidationService {
  /** 在 final_test_score 已提交后构建 VALIDATE 最终结论。 */
  buildResult(opts: {
    testScore: number
    finalTestScore: number
    direction: string
  }): ValidationResult {
    const gap = generalizationGap(opts.testScore, opts.finalTestScore, opts.direction)
    return ValidationResultSchema.parse({
      result_id: newId("vr"),
      status: "COMPLETED",
      test_score: opts.testScore,
      final_test_score: opts.finalTestScore,
      generalization_gap: gap,
      generalization_warning: generalizationWarning(gap),
    })
  }
}
