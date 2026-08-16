import { describe, expect, it } from "vitest"
import { BudgetService, parseDurationMs } from "../../src/index.js"

const budgets = {
  max_ideas: 5,
  max_experiments: 2,
  max_paper_rounds: 3,
  max_tokens: 10,
  project_time_limit: "PT1H",
}

const pool = { countQueued: () => 0, countByOrigin: () => 2 }

describe("BudgetService", () => {
  it("tracks token budget", () => {
    const service = new BudgetService(budgets, 0)
    expect(service.canSpendTokens(10)).toBe(true)
    service.addTokens(8)
    expect(service.canSpendTokens(3)).toBe(false)
  })

  it("treats max_tokens=0 as unlimited", () => {
    const service = new BudgetService({ ...budgets, max_tokens: 0 }, 0)
    expect(service.canSpendTokens(1_000_000)).toBe(true)
  })

  it("computes remaining ideas from pool origin counts", () => {
    const service = new BudgetService(budgets, 0)
    expect(service.ideasRemaining(pool)).toBe(3)
  })

  it("parses ISO-8601 PT durations", () => {
    expect(parseDurationMs("PT12H")).toBe(12 * 3_600_000)
    expect(parseDurationMs("PT1H30M")).toBe(5_400_000)
    expect(parseDurationMs("PT45S")).toBe(45_000)
    expect(() => parseDurationMs("12H")).toThrow(/unsupported duration/)
  })

  it("detects time limit", () => {
    const service = new BudgetService({ ...budgets, project_time_limit: "PT1S" }, 0)
    expect(service.timeLimitReached()).toBe(true)
  })
})
