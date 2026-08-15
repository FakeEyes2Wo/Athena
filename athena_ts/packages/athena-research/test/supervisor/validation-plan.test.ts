import { describe, expect, it } from "vitest"
import { PlanDecisionSchema } from "../../src/supervisor/plans.js"
import { runValidationPlan } from "../../src/supervisor/validation.js"

describe("runValidationPlan", () => {
  it("builds a ValidationResult on submit", async () => {
    const result = await runValidationPlan({
      agent: { turn: async () => PlanDecisionSchema.parse({ decision: "submit", reason: "done" }) },
      testScore: 0.8,
      direction: "maximize",
      maxTurns: 3,
      runFinalTest: async () => 0.75,
    })

    expect(result.status).toBe("COMPLETED")
    expect(result.test_score).toBe(0.8)
    expect(result.final_test_score).toBe(0.75)
    expect(result.generalization_gap).toBeCloseTo(0.05)
    expect(result.generalization_warning).toBe(true)
  })

  it("abandon raises", async () => {
    await expect(
      runValidationPlan({
        agent: { turn: async () => PlanDecisionSchema.parse({ decision: "abandon", reason: "nope" }) },
        testScore: 0.8,
        direction: "maximize",
        maxTurns: 2,
        runFinalTest: async () => 0.75,
      })
    ).rejects.toThrow(/abandoned/)
  })
})
