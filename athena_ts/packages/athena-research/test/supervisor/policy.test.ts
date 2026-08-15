import { describe, expect, it } from "vitest"
import { HypothesisSchema, ResearchTree } from "@athena/core"
import { EloPolicy, Outcome, queueOrder } from "../../src/supervisor/policy.js"

function hypothesis(priority: number, order = 0) {
  return HypothesisSchema.parse({
    statement: "claim",
    intervention: "change",
    expected_effect: "improve",
    priority,
    order,
  })
}

describe("policy", () => {
  it("single sided elo settlement uses fixed reference expectation", () => {
    const policy = new EloPolicy(32)
    expect(policy.settle(1048.0, Outcome.WIN)).toBe(1064.0)
    expect(policy.settle(1048.0, Outcome.DRAW)).toBe(1048.0)
    expect(policy.settle(1048.0, Outcome.LOSS)).toBe(1032.0)
  })

  it("root and child seed use the settled parent priority", () => {
    const policy = new EloPolicy()
    expect(policy.seed(null)).toBe(1000.0)
    expect(policy.seed(hypothesis(1016.0))).toBe(1016.0)
  })

  it("root priority is fixed", () => {
    expect(EloPolicy.ROOT_PRIORITY).toBe(1000.0)
  })

  it("policy priority reads hypothesis priority", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(hypothesis(1032.0))
    expect(new EloPolicy().priority(tree.pendingHypotheses()[0]!, tree)).toBe(1032.0)
  })

  it.each([0.0, -1.0, NaN, Infinity, -Infinity])(
    "elo k must be positive and finite (%j)",
    (k) => {
      expect(() => new EloPolicy(k)).toThrow(/positive finite/)
    }
  )

  it.each([NaN, Infinity, -Infinity])(
    "elo reference priority must be finite (%j)",
    (reference) => {
      expect(() => new EloPolicy().settle(reference, Outcome.WIN)).toThrow(/finite/)
    }
  )

  it.each([NaN, Infinity, -Infinity])(
    "queue order rejects nonfinite priority (%j)",
    (priority) => {
      expect(() => queueOrder(priority, 0)).toThrow(/finite/)
    }
  )

  it("queue order is priority descending then fifo order ascending", () => {
    const hypotheses = [
      hypothesis(1000.0, 2),
      hypothesis(1016.0, 3),
      hypothesis(1000.0, 1),
    ]
    const ordered = [...hypotheses].sort((a, b) => {
      const [pa, oa] = queueOrder(a.priority, a.order)
      const [pb, ob] = queueOrder(b.priority, b.order)
      if (pa !== pb) return pa - pb
      return oa - ob
    })
    expect(ordered.map((item) => [item.priority, item.order])).toEqual([
      [1016.0, 3],
      [1000.0, 1],
      [1000.0, 2],
    ])
  })
})
