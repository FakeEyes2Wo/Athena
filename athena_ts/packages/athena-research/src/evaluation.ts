/**
 * TrustedEvaluator — 唯一可信 test/final-test evaluator（移植 ``research/evaluation.py``）。
 */

import { CandidateEvaluationSchema, type CandidateEvaluation, type DataScriptBundle } from "./contracts.js"
import type { ScriptRunResult } from "./script_runner.js"

/** 候选输出导致的评分失败（等价 Python ValueError → scoring_failed）。 */
export class ScoringError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "ScoringError"
  }
}

export interface EvaluatorRunner {
  run(
    bundle: DataScriptBundle,
    request: Record<string, unknown>,
    outputSchema?: Record<string, unknown> | null,
    extraFiles?: Record<string, Uint8Array> | null
  ): Promise<ScriptRunResult>
}

/** PlanRunner 依赖的可信打分接口。 */
export interface Scorer {
  score(opts: {
    evalBundle: DataScriptBundle
    predictions: Record<string, Uint8Array>
    candidateId: string
    direction: "maximize" | "minimize"
    predictionsRoot: string
  }): Promise<CandidateEvaluation>
}

export class TrustedEvaluator implements Scorer {
  private runner: EvaluatorRunner

  constructor(runner: EvaluatorRunner) {
    this.runner = runner
  }

  /** 运行 eval 入口，只注入 predictions 目录；labels 来自冻结 bundle。 */
  async score(opts: {
    evalBundle: DataScriptBundle
    predictions: Record<string, Uint8Array>
    candidateId: string
    direction: "maximize" | "minimize"
    predictionsRoot: string
  }): Promise<CandidateEvaluation> {
    let result: ScriptRunResult
    try {
      const extraFiles: Record<string, Uint8Array> = {}
      for (const [rel, content] of Object.entries(opts.predictions)) {
        extraFiles[`${opts.predictionsRoot}/${rel}`] = content
      }
      result = await this.runner.run(opts.evalBundle, {}, { primary: null }, extraFiles)
    } catch (exc) {
      throw new ScoringError(
        `evaluator failed to produce a score: ${exc instanceof Error ? exc.message : String(exc)}`
      )
    }

    const primary = result.outputs["primary"]
    if (primary === undefined || primary === null) {
      throw new ScoringError("candidate evaluation produced no primary score")
    }
    if (typeof primary === "boolean" || (typeof primary !== "number" && typeof primary !== "string")) {
      throw new ScoringError(`primary score must be a number, got ${typeof primary}`)
    }
    let metric: number
    try {
      metric = Number(primary)
    } catch {
      throw new ScoringError(`primary score must be numeric, got ${JSON.stringify(primary)}`)
    }
    if (!Number.isFinite(metric)) {
      throw new ScoringError(`primary score must be finite, got ${JSON.stringify(primary)}`)
    }
    return CandidateEvaluationSchema.parse({
      candidate_id: opts.candidateId,
      test_score: metric,
      direction: opts.direction,
    })
  }
}
