/**
 * VALIDATE 阶段执行（移植 ``research/supervisor/validation.py`` 的 final-test 编排，简化版）。
 */

import { ValidationResultSchema, type ValidationResult } from "../contracts.js"
import { ValidationService } from "../validation.js"
import type { AgentTurn } from "./prepare.js"

/** 运行 validate agent 直到产出 final-test 分数并构建 ValidationResult。 */
export async function runValidationPlan(opts: {
  agent: AgentTurn
  testScore: number
  direction: "maximize" | "minimize"
  maxTurns: number
  runFinalTest: () => Promise<number>
}): Promise<ValidationResult> {
  if (opts.maxTurns < 1) throw new Error("max_turns must be at least 1")
  let feedback: string | null = null
  for (let turn = 0; turn < opts.maxTurns; turn++) {
    const prompt = feedback ?? "Run the final-test evaluation and submit."
    const decision = await opts.agent.turn(prompt)
    if (decision === null) {
      feedback = "previous validate turn did not produce a valid decision"
      continue
    }
    if (decision.decision === "abandon") {
      throw new Error(`validate Agent abandoned Plan: ${decision.reason}`)
    }
    try {
      const finalTestScore = await opts.runFinalTest()
      if (decision.decision !== "submit") {
        throw new Error(`final-test scored successfully but the decision was ${decision.decision}`)
      }
      return new ValidationService().buildResult({
        testScore: opts.testScore,
        finalTestScore,
        direction: opts.direction,
      })
    } catch (exc) {
      feedback = (exc as Error).message
    }
  }
  throw new Error("validate turn budget exhausted without a final-test result")
}

export { ValidationResultSchema }
