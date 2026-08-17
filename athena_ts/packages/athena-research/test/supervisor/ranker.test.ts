import { describe, expect, it } from "vitest"
import {
  ExperimentPlanSchema,
  ExperimentSchema,
  HypothesisSchema,
  ResearchTree,
} from "@athena/core"
import { EloPolicy } from "../../src/supervisor/policy.js"
import { Selector, deduplicate, jaccard, tokenize } from "../../src/supervisor/ranker.js"

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
