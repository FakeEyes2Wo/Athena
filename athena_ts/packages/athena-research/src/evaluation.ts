/**
 * TrustedEvaluator — 唯一可信 test/final-test evaluator（移植 ``research/evaluation.py``）。
 */

import { CandidateEvaluationSchema, type CandidateEvaluation, type DataScriptBundle } from "./contracts.js"
import type { DataScriptRunner } from "./script_runner.js"

/** 候选输出导致的评分失败（等价 Python ValueError → scoring_failed）。 */
export class ScoringError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "ScoringError"
  }
}

export type EvaluatorRunner = Pick<DataScriptRunner, "run">

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
  constructor(private readonly runner: EvaluatorRunner) {}

  /** 运行 eval 入口，只注入 predictions 目录；labels 来自冻结 bundle。 */
  async score(opts: Parameters<Scorer["score"]>[0]): Promise<CandidateEvaluation> {
    let result: Record<string, unknown>
    try {
      const extraFiles: Record<string, Uint8Array> = {}
      for (const [rel, content] of Object.entries(opts.predictions)) {
        extraFiles[`${opts.predictionsRoot}/${rel}`] = content
      }
      result = await this.runner.run(opts.evalBundle, extraFiles)
    } catch (exc) {
      throw new ScoringError(
        `evaluator failed to produce a score: ${exc instanceof Error ? exc.message : String(exc)}`
      )
    }

    const primary = result["primary"]
    if (primary === undefined || primary === null) {
      throw new ScoringError("candidate evaluation produced no primary score")
    }
    if (typeof primary !== "number" && typeof primary !== "string") {
      throw new ScoringError(`primary score must be a number, got ${typeof primary}`)
    }
    const metric = Number(primary)
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
