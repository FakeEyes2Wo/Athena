import { describe, expect, it, vi } from "vitest"
import {
  ExperimentPlanSchema,
  ExperimentSchema,
  HypothesisSchema,
  ResearchTree,
} from "@athena/core"
import { DEFAULT_RANK_CONFIG, EloPolicy, Selector, deduplicate, jaccard, tokenize } from "../../src/supervisor/ranker.js"

const REF = "sha256:" + "a".repeat(64)

function hypothesis(statement: string, intervention: string, opts: { priority?: number; order?: number; sources?: string[] } = {}) {
  return HypothesisSchema.parse({
    statement,
    intervention,
    expected_effect: "improve",
    priority: opts.priority ?? 1000.0,
    order: opts.order ?? 0,
    sources: opts.sources ?? [],
  })
}

describe("ranker", () => {
  it.each([
    DEFAULT_RANK_CONFIG,
    { priorWeight: 0.2, strengthWeight: 0.6, noveltyWeight: 0.1, costWeight: 0.4, dedupThreshold: 1 },
  ])("preserves weighted scoring across mixed evidence, strength and clipped costs", (config) => {
    const tree = new ResearchTree()
    const candidates = Array.from({ length: 12 }, (_, order) => {
      const id = tree.addHypothesis(hypothesis(`claim ${order}`, order % 2 ? "one" : "train deeper model", {
        order,
        priority: 1000 + order * 10,
        sources: order % 3 ? [] : ["source"],
      }))
      const candidate = tree.getHypothesis(id)
      candidate.cost = order % 4 * 0.5
      return candidate
    })
    const expected = candidates.map((candidate, order) => ({
      candidate,
      score: config.priorWeight * (0.4 + (order % 3 ? 0 : 0.3) + (order % 2 ? 0 : 0.3)) +
        config.strengthWeight * order / 11 + config.noveltyWeight -
        config.costWeight * Math.min(order % 4 * 0.5, 1),
    })).sort((a, b) => b.score - a.score).map(({ candidate }) => candidate)
    expect(new Selector(new EloPolicy(), config).rank(tree, candidates)).toEqual(expected)
  })

  it("recomputes scores on the next ranking rather than retaining a stale cache", () => {
    const tree = new ResearchTree()
    const candidates = ["first", "second"].map((name, order) => {
      const id = tree.addHypothesis(hypothesis(name, "one", { order }))
      return tree.getHypothesis(id)
    })
    const selector = new Selector(new EloPolicy())
    candidates[1]!.cost = 1
    expect(selector.rank(tree, candidates)[0]).toBe(candidates[0])
    candidates[0]!.cost = 1
    candidates[1]!.cost = 0
    expect(selector.rank(tree, candidates)[0]).toBe(candidates[1])
  })

  it.each([
    { priorWeight: -0.1 }, { strengthWeight: 1.1 }, { noveltyWeight: -0.1 },
    { costWeight: -0.1 }, { dedupThreshold: 0 }, { dedupThreshold: 1.1 },
  ])("retains configuration range checks: %j", (invalid) => {
    expect(() => new Selector(new EloPolicy(), { ...DEFAULT_RANK_CONFIG, ...invalid })).toThrow()
  })

  it("reads canonical history once per ranking and does not mutate candidates", () => {
    const tree = new ResearchTree()
    const candidates = Array.from({ length: 8 }, (_, order) => {
      const id = tree.addHypothesis(hypothesis(`claim ${order}`, `train model ${order}`, { order }))
      return tree.getHypothesis(id)
    }).reverse()
    const before = candidates.map((candidate) => candidate.id)
    const snapshot = vi.spyOn(tree, "toDict")
    const priority = vi.fn((candidate) => candidate.priority)
    const ranked = new Selector({ priority }).rank(tree, candidates)
    expect(ranked.map((candidate) => candidate.order)).toEqual([0, 1, 2, 3, 4, 5, 6, 7])
    expect(candidates.map((candidate) => candidate.id)).toEqual(before)
    expect(priority).toHaveBeenCalledTimes(8)
    expect(snapshot).toHaveBeenCalledTimes(1)
  })

  it("handles empty, unregistered, and equal-score candidates", () => {
    const selector = new Selector(new EloPolicy())
    const tree = new ResearchTree()
    expect(selector.rank(tree, [])).toEqual([])
    const first = hypothesis("first", "one", { order: 1 })
    const second = hypothesis("second", "two", { order: 2 })
    expect(selector.rank(tree, [second, first])).toEqual([first, second])
    expect(selector.rank(tree, [first])).toEqual([first])
  })

  it("deduplicates against existing hypotheses without changing either input", () => {
    const existing = [hypothesis("same", "change")]
    const different = hypothesis("novel", "approach")
    const candidates = [hypothesis("SAME", "CHANGE"), different]
    expect(deduplicate(candidates, existing, 1)).toEqual([different])
    expect(existing).toHaveLength(1)
    expect(candidates).toHaveLength(2)
    expect(jaccard(new Set(), new Set())).toBe(0)
    expect(jaccard(new Set(["a"]), new Set())).toBe(0)
  })

  it.each(["priorWeight", "strengthWeight", "noveltyWeight", "costWeight", "dedupThreshold"] as const)(
    "rejects non-finite %s at the configuration boundary", (field) => {
      for (const value of [NaN, Infinity, -Infinity]) {
        expect(() => new Selector(new EloPolicy(), { ...DEFAULT_RANK_CONFIG, [field]: value })).toThrow()
      }
    }
  )

  it("tokenizes and computes jaccard similarity", () => {
    expect(tokenize("Add BatchNorm layers")).toEqual(new Set(["add", "batchnorm", "layers"]))
    expect(jaccard(new Set(["a", "b"]), new Set(["b", "c"]))).toBeCloseTo(1 / 3)
  })

  it("deduplicates near-identical candidates in input order", () => {
    const kept = deduplicate(
      [hypothesis("add dropout", "add dropout 0.5"), hypothesis("add dropout", "add dropout 0.6"), hypothesis("calibrate features", "add calibrated features")],
      [],
      0.4
    )
    expect(kept.map((item) => item.statement)).toEqual(["add dropout", "calibrate features"])
  })

  it("ranks by rubric prior, policy strength and FIFO tie-break", () => {
    const tree = new ResearchTree()
    const high = tree.addHypothesis(hypothesis("evidence-backed direction", "train deeper residual model", { priority: 1100, order: 2, sources: ["https://example.test/paper"] }))
    const low = tree.addHypothesis(hypothesis("plain direction", "change learning rate", { priority: 900, order: 1 }))

    const selector = new Selector(new EloPolicy())
    const ranked = selector.rank(tree, [
      tree.getHypothesis(low),
      tree.getHypothesis(high),
    ])

    expect(ranked.map((item) => item.id)).toEqual([high, low])
  })

  it("novelty rewards hypotheses unlike settled experiments", () => {
    const tree = new ResearchTree()
    const baseline = tree.addHypothesis(
      HypothesisSchema.parse({
        id: "baseline",
        statement: "dropout regularization baseline",
        intervention: "add dropout regularization",
        expected_effect: "reference",
      })
    )
    tree.addExperiment(
      "exp_baseline",
      ExperimentSchema.parse({
        hypothesis_id: baseline,
        commit: "c0",
        plan: ExperimentPlanSchema.parse({
          kind: "baseline",
          change: "baseline",
          run_config_ref: REF,
          budget: {},
          acceptance_rule: "trusted score",
        }),
        gitwork: { path: "ws", branch: "main", base_commit: "c0" },
        status: "SUCCEEDED",
        eval: { experiment_id: "exp_baseline", primary: 0.8, per_sample: REF },
      })
    )
    tree.setSota("exp_baseline")
    tree.updateHypothesisStatus("baseline", "SUPPORTED")

    const selector = new Selector(new EloPolicy())
    const sameDirection = tree.addHypothesis(hypothesis("more dropout", "add more dropout regularization", { priority: 1000, order: 1 }))
    const differentDirection = tree.addHypothesis(hypothesis("new direction", "train a vision transformer head", { priority: 1000, order: 2 }))
    const ranked = selector.rank(tree, [tree.getHypothesis(sameDirection), tree.getHypothesis(differentDirection)])

    expect(ranked[0]!.id).toBe(differentDirection)
  })
})
