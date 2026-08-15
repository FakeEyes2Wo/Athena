import { describe, expect, it } from "vitest"
import { ValidationService, generalizationGap, generalizationWarning } from "../src/validation.js"

describe("validation", () => {
  it("generalization gap maximize is test minus final", () => {
    expect(generalizationGap(0.8, 0.75, "maximize")).toBeCloseTo(0.05)
    expect(generalizationGap(0.8, 0.85, "maximize")).toBeCloseTo(-0.05)
  })

  it("generalization gap minimize is final minus test", () => {
    expect(generalizationGap(0.1, 0.12, "minimize")).toBeCloseTo(0.02)
    expect(generalizationGap(0.12, 0.1, "minimize")).toBeCloseTo(-0.02)
  })

  it("generalization warning ignores numeric noise", () => {
    expect(generalizationWarning(1e-13)).toBe(false)
    expect(generalizationWarning(0.05)).toBe(true)
  })

  it("build result warns but still completes", () => {
    const result = new ValidationService().buildResult({
      testScore: 0.8,
      finalTestScore: 0.7,
      direction: "maximize",
    })
    expect(result.status).toBe("COMPLETED")
    expect(result.generalization_gap).toBeCloseTo(0.1)
    expect(result.generalization_warning).toBe(true)
  })

  it("build result without regression has no warning", () => {
    const result = new ValidationService().buildResult({
      testScore: 0.8,
      finalTestScore: 0.82,
      direction: "maximize",
    })
    expect(result.generalization_gap).toBeCloseTo(-0.02)
    expect(result.generalization_warning).toBe(false)
  })
})
