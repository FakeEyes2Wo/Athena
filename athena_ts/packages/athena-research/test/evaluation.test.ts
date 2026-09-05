import { describe, expect, it, vi } from "vitest"
import { ScoringError, TrustedEvaluator } from "../src/evaluation.js"
import { DataScriptBundleSchema } from "../src/contracts.js"

function evaluator(outputs: Record<string, unknown>): TrustedEvaluator {
  return new TrustedEvaluator({ run: async () => outputs })
}

function score(primary: unknown, direction: "maximize" | "minimize" = "maximize") {
  return evaluator({ primary }).score({
    evalBundle: null as never,
    predictions: {},
    candidateId: "c",
    direction,
    predictionsRoot: "predictions",
  })
}

describe("TrustedEvaluator", () => {
  it("rejects missing primary scores", async () => {
    for (const missing of [undefined, null]) {
      await expect(score(missing)).rejects.toThrow(/no primary score/)
    }
  })

  it.each([new Error("failed"), "failed"])("normalizes runner failures: %s", async (failure) => {
    const evaluator = new TrustedEvaluator({ run: async () => { throw failure } })
    await expect(evaluator.score({
      evalBundle: DataScriptBundleSchema.parse({ bundle_id: "b", entrypoint: "eval.py" }),
      predictions: {}, candidateId: "c", direction: "maximize", predictionsRoot: "predictions",
    })).rejects.toEqual(new ScoringError("evaluator failed to produce a score: failed"))
  })

  it("forwards frozen bundle and prediction files while retaining score identity and direction", async () => {
    const run = vi.fn().mockResolvedValue({ primary: "-0.25" })
    const bundle = DataScriptBundleSchema.parse({ bundle_id: "b", entrypoint: "eval.py" })
    const predictions = { "nested/data.csv": Buffer.from("prediction") }
    const result = await new TrustedEvaluator({ run }).score({
      evalBundle: bundle, predictions, candidateId: "candidate",
      direction: "minimize", predictionsRoot: "outputs",
    })
    expect(run).toHaveBeenCalledWith(bundle, {}, { primary: null }, {
      "outputs/nested/data.csv": predictions["nested/data.csv"],
    })
    expect(result).toMatchObject({ candidate_id: "candidate", direction: "minimize", test_score: -0.25 })
    expect(Object.keys(predictions)).toEqual(["nested/data.csv"])
  })

  it("rejects non finite metric", async () => {
    for (const bad of [NaN, Infinity, -Infinity, "nan", "Infinity"]) {
      await expect(score(bad)).rejects.toThrow(/finite/)
    }
  })

  it("rejects non scalar metric", async () => {
    for (const bad of [[0.8], { x: 1 }, true]) {
      await expect(score(bad)).rejects.toThrow(/number/)
    }
  })

  it("accepts finite number and numeric string", async () => {
    const result = await score(0.84)
    expect(result.test_score).toBe(0.84)
    expect(Number.isFinite(result.test_score)).toBe(true)

    const result2 = await score("0.84")
    expect(result2.test_score).toBe(0.84)
  })
})
