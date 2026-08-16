import { describe, expect, it } from "vitest"
import { HypothesisSchema, ResearchTree } from "@athena/core"
import { HypothesisPool, PooledHypothesisSchema } from "../../src/index.js"
import { makeTmpDir } from "../_support.js"

function entry(poolId: string, status = "QUEUED") {
  return PooledHypothesisSchema.parse({
    pool_id: poolId,
    hypothesis: HypothesisSchema.parse({
      id: poolId,
      statement: `claim ${poolId}`,
      intervention: `change ${poolId}`,
      expected_effect: "improve",
    }),
    pool_status: status,
    origin: "test",
    created_at: new Date(0).toISOString(),
    updated_at: new Date(0).toISOString(),
  })
}

describe("HypothesisPool", () => {
  it("persists and reloads entries", () => {
    const path = `${makeTmpDir()}/pool.json`
    const pool = new HypothesisPool(path)
    pool.upsert(entry("h1"))
    pool.save()
    const reloaded = new HypothesisPool(path)
    reloaded.load()
    expect(reloaded.get("h1").pool_id).toBe("h1")
  })

  it("records settlement status transitions", () => {
    const pool = new HypothesisPool(`${makeTmpDir()}/pool.json`)
    pool.upsert(entry("h1"))
    pool.recordSettlement({
      poolId: "h1",
      outcome: "WIN",
      metric: 0.86,
      referenceMetric: 0.8,
      direction: "maximize",
      experimentId: "exp_h1",
    })
    const updated = pool.get("h1")
    expect(updated.pool_status).toBe("SUPPORTED")
    expect(updated.best_metric).toBe(0.86)
    expect(updated.delta_vs_reference).toBeCloseTo(0.06)
    expect(updated.experiment_ids).toEqual(["exp_h1"])
  })

  it("records negative results for paper", () => {
    const pool = new HypothesisPool(`${makeTmpDir()}/pool.json`)
    pool.upsert(entry("h1", "REFUTED"))
    pool.recordPaperPromotion({
      poolId: "h1",
      paperRefs: ["sections/limitations.tex"],
      sectionIds: ["limitations"],
      negativeResult: true,
    })
    expect(pool.get("h1").pool_status).toBe("NEGATIVE_RESULT")
    expect(pool.negativeResults().length).toBe(1)
  })

  it("reconciles missing entries from ResearchTree", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(
      HypothesisSchema.parse({
        id: "h_tree",
        statement: "tree claim",
        intervention: "tree change",
        expected_effect: "improve",
        status: "PROPOSED",
      }),
    )
    const pool = new HypothesisPool(`${makeTmpDir()}/pool.json`)
    pool.reconcile(tree)
    expect(pool.has("h_tree")).toBe(true)
    expect(pool.get("h_tree").origin).toBe("research_tree_import")
  })
})
