import { describe, expect, it, vi } from "vitest"
import { PlanDecisionSchema } from "../../src/contracts.js"
import { runValidationPlan } from "../../src/supervisor/validation.js"

describe("runValidationPlan", () => {
  it.each([
    ["maximize", 0.8, 0.75, 0.05, true],
    ["maximize", 0.8, 0.85, -0.05, false],
    ["minimize", 0.1, 0.12, 0.02, true],
    ["minimize", 0.12, 0.1, -0.02, false],
    ["maximize", 1e-13, 0, 1e-13, false],
    ["maximize", 1e-12, 0, 1e-12, false],
    ["maximize", 2e-12, 0, 2e-12, true],
    ["minimize", 0, 0, 0, false],
  ] as const)("computes %s gap from %s to %s", async (direction, testScore, finalTestScore, gap, warning) => {
    const result = await runValidationPlan({
      agent: { turn: async () => PlanDecisionSchema.parse({ decision: "submit", reason: "done" }) },
      testScore, direction, maxTurns: 1, runFinalTest: async () => finalTestScore,
    })
    expect(result.generalization_gap).toBeCloseTo(gap, 14)
    expect(result.generalization_warning).toBe(warning)
    expect(result.status).toBe("COMPLETED")
    expect(result.result_id).toMatch(/^vr/)
  })

  it("retries invalid decisions and scorer failures with feedback", async () => {
    const submit = PlanDecisionSchema.parse({ decision: "submit", reason: "done" })
    const turn = vi.fn().mockResolvedValueOnce(null).mockResolvedValue(submit)
    const runFinalTest = vi.fn().mockRejectedValueOnce(new Error("retry scorer")).mockResolvedValue(0.75)
    const result = await runValidationPlan({
      agent: { turn }, testScore: 0.8, direction: "maximize", maxTurns: 3, runFinalTest,
    })
    expect(turn.mock.calls.map(([prompt]) => prompt)).toEqual([
      "Run the final-test evaluation and submit.",
      "previous validate turn did not produce a valid decision",
      "retry scorer",
    ])
    expect(runFinalTest).toHaveBeenCalledTimes(2)
    expect(result.final_test_score).toBe(0.75)
  })

  it("preserves scoring-before-submit-check and reports a continue decision", async () => {
    const turn = vi.fn()
      .mockResolvedValueOnce(PlanDecisionSchema.parse({ decision: "continue", reason: "working" }))
      .mockResolvedValue(PlanDecisionSchema.parse({ decision: "submit", reason: "done" }))
    const runFinalTest = vi.fn().mockResolvedValue(0.75)
    await runValidationPlan({ agent: { turn }, testScore: 0.8, direction: "maximize", maxTurns: 2, runFinalTest })
    expect(runFinalTest).toHaveBeenCalledTimes(2)
    expect(turn).toHaveBeenLastCalledWith("final-test scored successfully but the decision was continue")
  })

  it("exhausts the turn budget without running a scorer for missing decisions", async () => {
    const runFinalTest = vi.fn()
    const turn = vi.fn().mockResolvedValue(null)
    await expect(runValidationPlan({
      agent: { turn }, testScore: 0.8, direction: "maximize", maxTurns: 2, runFinalTest,
    })).rejects.toThrow("turn budget exhausted")
    expect(turn).toHaveBeenCalledTimes(2)
    expect(runFinalTest).not.toHaveBeenCalled()
  })

  it("rejects a zero budget before invoking either port", async () => {
    const turn = vi.fn()
    const runFinalTest = vi.fn()
    await expect(runValidationPlan({
      agent: { turn }, testScore: 0.8, direction: "maximize", maxTurns: 0, runFinalTest,
    })).rejects.toThrow("at least 1")
    expect(turn).not.toHaveBeenCalled()
    expect(runFinalTest).not.toHaveBeenCalled()
  })

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
