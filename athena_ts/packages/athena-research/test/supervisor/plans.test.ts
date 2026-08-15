import { describe, expect, it } from "vitest"
import { HypothesisSchema, parseOrThrow } from "@athena/core"
import {
  PlanBestSchema,
  PlanDecisionSchema,
  PlanInputSchema,
  PlanStateSchema,
  planStateToJSON,
} from "../../src/supervisor/plans.js"

const TRUSTED_REF = "sha256:" + "a".repeat(64)
const OTHER_TRUSTED_REF = "sha256:" + "c".repeat(64)

describe("PlanDecision", () => {
  it("requires an explicit structured decision", () => {
    expect(() => parseOrThrow(PlanDecisionSchema, { reason: "I give up" })).toThrow()
  })

  it.each(["continue", "submit", "abandon"])("accepts %s", (decision) => {
    const parsed = parseOrThrow(PlanDecisionSchema, { decision, reason: "done" })
    expect(parsed.decision).toBe(decision)
    expect(parsed.suggestions).toEqual([])
  })
})

describe("PlanInput", () => {
  it("rejects negative initial limits", () => {
    expect(() =>
      parseOrThrow(PlanInputSchema, {
        evaluator_ref: TRUSTED_REF,
        tree_ref: OTHER_TRUSTED_REF,
        initial_turn_limit: -1,
      })
    ).toThrow()
  })

  it("json round trip freezes metric comparison", () => {
    const planInput = parseOrThrow(PlanInputSchema, {
      evaluator_ref: TRUSTED_REF,
      tree_ref: OTHER_TRUSTED_REF,
      direction: "minimize",
      tolerance: 0.01,
    })
    const loaded = parseOrThrow(PlanInputSchema, JSON.parse(JSON.stringify(planInput)))
    expect(loaded.direction).toBe("minimize")
    expect(loaded.tolerance).toBe(0.01)
  })

  it.each([-0.01, Infinity, NaN])("rejects invalid metric tolerance (%j)", (tolerance) => {
    expect(() =>
      parseOrThrow(PlanInputSchema, {
        evaluator_ref: TRUSTED_REF,
        tree_ref: OTHER_TRUSTED_REF,
        tolerance,
      })
    ).toThrow()
  })

  it.each(["initial_turn_limit", "initial_patience"])(
    "rejects coerced numeric limits (%s)",
    (field) => {
      expect(() =>
        parseOrThrow(PlanInputSchema, {
          evaluator_ref: TRUSTED_REF,
          tree_ref: OTHER_TRUSTED_REF,
          [field]: "4",
        })
      ).toThrow()
    }
  )

  it("rejects coerced nested hypothesis counter", () => {
    expect(() =>
      parseOrThrow(PlanInputSchema, {
        hypothesis: {
          statement: "Try robust scaling",
          intervention: "Replace standard scaling",
          expected_effect: "Improve validation score",
          patience: "4",
        },
        evaluator_ref: TRUSTED_REF,
        tree_ref: OTHER_TRUSTED_REF,
      })
    ).toThrow()
  })

  it("rejects unknown nested hypothesis field", () => {
    expect(() =>
      parseOrThrow(PlanInputSchema, {
        hypothesis: {
          statement: "Try robust scaling",
          intervention: "Replace standard scaling",
          expected_effect: "Improve validation score",
          unknown: "ignored",
        },
        evaluator_ref: TRUSTED_REF,
        tree_ref: OTHER_TRUSTED_REF,
      })
    ).toThrow()
  })

  it("owns deeply immutable hypothesis snapshots (deep copy)", () => {
    const source = HypothesisSchema.parse({
      statement: "Try robust scaling",
      intervention: "Replace standard scaling",
      expected_effect: "Improve validation score",
    })
    const planInput = parseOrThrow(PlanInputSchema, {
      hypothesis: source,
      active_ancestor_hypotheses: [source],
      evaluator_ref: TRUSTED_REF,
      tree_ref: OTHER_TRUSTED_REF,
    })

    source.statement = "Mutated source"

    expect(planInput.hypothesis!.statement).toBe("Try robust scaling")
    expect(planInput.active_ancestor_hypotheses[0]!.statement).toBe("Try robust scaling")
  })

  it("source supersedes mutation does not change snapshot", () => {
    const source = HypothesisSchema.parse({
      statement: "Try robust scaling",
      intervention: "Replace standard scaling",
      expected_effect: "Improve validation score",
      supersedes: ["hyp_old"],
    })
    const planInput = parseOrThrow(PlanInputSchema, {
      hypothesis: source,
      active_ancestor_hypotheses: [source],
      evaluator_ref: TRUSTED_REF,
      tree_ref: OTHER_TRUSTED_REF,
    })

    source.supersedes.push("hyp_new")

    expect(planInput.hypothesis!.supersedes).toEqual(["hyp_old"])
    expect(planInput.active_ancestor_hypotheses[0]!.supersedes).toEqual(["hyp_old"])
  })
})

describe("PlanState", () => {
  it("search plan requires patience", () => {
    expect(() =>
      parseOrThrow(PlanStateSchema, {
        kind: "SEARCH",
        context_ref: TRUSTED_REF,
        turns_used: 0,
        turn_limit: 12,
      })
    ).toThrow(/SEARCH Plan requires patience/)
  })

  it.each(["PREPARE", "VALIDATE"])("non-search plan rejects search-only fields (%s)", (kind) => {
    expect(() =>
      parseOrThrow(PlanStateSchema, {
        kind,
        context_ref: TRUSTED_REF,
        turns_used: 0,
        turn_limit: 12,
        patience: 4,
      })
    ).toThrow(/SEARCH-only/)
  })

  it.each([
    ["turns_used", -1],
    ["turn_limit", -1],
    ["patience", -1],
    ["stale_rounds", -1],
  ])("rejects negative counters (%s)", (field, value) => {
    const payload: Record<string, unknown> = {
      kind: "SEARCH",
      context_ref: TRUSTED_REF,
      turns_used: 0,
      turn_limit: 12,
      patience: 4,
      [field]: value,
    }
    expect(() => parseOrThrow(PlanStateSchema, payload)).toThrow()
  })

  it("rejects untrusted best reference", () => {
    expect(() =>
      parseOrThrow(PlanStateSchema, {
        kind: "SEARCH",
        context_ref: TRUSTED_REF,
        turns_used: 1,
        turn_limit: 12,
        patience: 4,
        best_ref: "artifact:claimed-best",
      })
    ).toThrow(/artifact reference/)
  })

  it.each([
    ["turns_used", "1"],
    ["turn_limit", "12"],
    ["patience", "4"],
    ["stale_rounds", "0"],
  ])("rejects coerced numeric counters (%s)", (field, value) => {
    expect(() =>
      parseOrThrow(PlanStateSchema, {
        kind: "SEARCH",
        context_ref: TRUSTED_REF,
        turns_used: 1,
        turn_limit: 12,
        patience: 4,
        [field]: value,
      })
    ).toThrow()
  })

  it.each([
    ["patience", null],
    ["stale_rounds", 0],
    ["best_ref", null],
  ])("non-search plan rejects explicit search defaults (%s)", (field, value) => {
    expect(() =>
      parseOrThrow(PlanStateSchema, {
        kind: "PREPARE",
        context_ref: TRUSTED_REF,
        turns_used: 0,
        turn_limit: 12,
        [field]: value,
      })
    ).toThrow(/SEARCH-only/)
  })

  it.each(["PREPARE", "VALIDATE"])("non-search serialization omits search fields (%s)", (kind) => {
    const plan = parseOrThrow(PlanStateSchema, {
      kind,
      context_ref: TRUSTED_REF,
      turns_used: 0,
      turn_limit: 12,
    })
    const json = planStateToJSON(plan)
    expect(Object.keys(json).sort()).toEqual([
      "context_ref",
      "kind",
      "turn_limit",
      "turns_used",
    ])
  })

  it("search serialization keeps durable fields", () => {
    const plan = parseOrThrow(PlanStateSchema, {
      kind: "SEARCH",
      context_ref: TRUSTED_REF,
      turns_used: 0,
      turn_limit: 12,
      patience: 4,
      stale_rounds: 1,
      best_ref: TRUSTED_REF,
    })
    expect(Object.keys(planStateToJSON(plan)).sort()).toEqual([
      "best_ref",
      "context_ref",
      "kind",
      "patience",
      "stale_rounds",
      "turn_limit",
      "turns_used",
    ])
  })
})

describe("PlanBest", () => {
  it("requires trusted evidence and nonblank commit", () => {
    const best = parseOrThrow(PlanBestSchema, {
      metric: 0.82,
      commit: "abc123",
      evidence_ref: TRUSTED_REF,
    })
    expect(best.evidence_ref).toBe(TRUSTED_REF)
    expect(() =>
      parseOrThrow(PlanBestSchema, { metric: 0.82, commit: "", evidence_ref: TRUSTED_REF })
    ).toThrow()
    expect(() =>
      parseOrThrow(PlanBestSchema, {
        metric: 0.82,
        commit: "abc123",
        evidence_ref: "artifact:evidence",
      })
    ).toThrow(/artifact reference/)
  })

  it.each([NaN, Infinity, -Infinity])("rejects non finite metric (%j)", (metric) => {
    expect(() =>
      parseOrThrow(PlanBestSchema, { metric, commit: "abc123", evidence_ref: TRUSTED_REF })
    ).toThrow()
  })

  it("rejects coerced metric", () => {
    expect(() =>
      parseOrThrow(PlanBestSchema, {
        metric: "0.82",
        commit: "abc123",
        evidence_ref: TRUSTED_REF,
      })
    ).toThrow()
  })
})
