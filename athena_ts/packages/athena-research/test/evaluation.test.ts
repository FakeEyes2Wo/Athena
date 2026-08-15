import { describe, expect, it } from "vitest"
import { TrustedEvaluator, type EvaluatorRunner } from "../src/evaluation.js"
import { ScriptRunResult } from "../src/script_runner.js"

class FakeRunner implements EvaluatorRunner {
  constructor(private outputs: Record<string, unknown>) {}
  async run(): Promise<ScriptRunResult> {
    return new ScriptRunResult([], this.outputs, false)
  }
}

function evaluator(outputs: Record<string, unknown>): TrustedEvaluator {
  return new TrustedEvaluator(new FakeRunner(outputs))
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
