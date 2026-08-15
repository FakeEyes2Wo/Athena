/**
 * PREPARE 阶段合同（移植 ``research/supervisor/prepare.py`` 的 PrepareResult 数据模型
 * 与 ``_freeze_evaluator`` 确定性冻结逻辑）。
 */

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs"
import { dirname, join } from "node:path"
import { z } from "zod"
import {
  ArtifactRef,
  CommitHash,
  FiniteFloat,
  type ArtifactStore,
  type GitWorkBranch,
  type GitWorkspace,
} from "@athena/core"

import { DataScriptBundleSchema, type DataScriptBundle } from "../contracts.js"
import { BundleMetadata, type DataScriptRunner } from "../script_runner.js"
import type { Scorer } from "../evaluation.js"
import type { ExecutionRuntime } from "../execution.js"
import { PlanRunner, type PlanTurnResult } from "./experiment.js"
import { PlanInputSchema, PlanStateSchema, type PlanDecision, type PlanState } from "./plans.js"

export const PREPARE_AGENT_ID = "prepare"
export const PREPARE_PLAN_ID = "prepare"
export const EVALUATOR_AGENT_ID = "evaluator"
export const EVALUATOR_PLAN_ID = "evaluator"

/** Trusted baseline data returned to the single-writer Supervisor。 */
export const PrepareResultSchema = z.strictObject({
  evaluator_ref: ArtifactRef,
  metric: FiniteFloat,
  commit: CommitHash,
  predictions_ref: ArtifactRef,
  evidence_ref: ArtifactRef,
  report_ref: ArtifactRef,
})
export type PrepareResult = z.infer<typeof PrepareResultSchema>

/** 冻结 evaluator 目录（metric.json 的 eval_script）为 bundle，返回 bundle JSON ref。 */
export async function freezeEvaluator(opts: {
  root: string
  scripts: DataScriptRunner
  store: ArtifactStore
}): Promise<ArtifactRef> {
  const { root, scripts, store } = opts
  const specPath = join(root, "metric.json")
  if (!existsSync(specPath)) throw new Error("metric.json is missing")
  let evaluatorRel: string
  try {
    const spec = JSON.parse(readFileSync(specPath, "utf-8")) as { eval_script?: unknown }
    if (typeof spec.eval_script !== "string") throw new Error()
    evaluatorRel = spec.eval_script
  } catch {
    throw new Error("metric.json must declare eval_script")
  }
  const evaluatorPath = join(root, evaluatorRel)
  let evaluatorRoot: string
  let entrypoint: string
  if (existsSync(evaluatorPath) && statSync(evaluatorPath).isDirectory()) {
    evaluatorRoot = evaluatorPath
    entrypoint = "evaluate.py"
    if (!existsSync(join(evaluatorRoot, entrypoint))) {
      throw new Error(`eval_script directory must contain an entrypoint file named evaluate.py: ${JSON.stringify(evaluatorRel)}`)
    }
  } else if (existsSync(evaluatorPath) && statSync(evaluatorPath).isFile()) {
    evaluatorRoot = dirname(evaluatorPath)
    entrypoint = evaluatorRel.split(/[\\/]/).pop()!
  } else {
    throw new Error("eval_script is missing")
  }

  const labelsFile = join(evaluatorRoot, "labels.csv")
  const labelsDir = join(evaluatorRoot, "labels")
  const hasLabels =
    (existsSync(labelsFile) && statSync(labelsFile).size > 0) ||
    (existsSync(labelsDir) && hasAnyFileSync(labelsDir))
  if (!hasLabels) {
    throw new Error(
      "eval labels are missing: labels.csv or a non-empty labels/ dir must sit next to the eval script"
    )
  }

  const bundle = await scripts.freeze(evaluatorRoot, new BundleMetadata(entrypoint))
  return store.putText(JSON.stringify(DataScriptBundleSchema.parse(bundle)))
}

function hasAnyFileSync(dir: string): boolean {
  let entries: string[]
  try {
    entries = readdirSync(dir)
  } catch {
    return false
  }
  for (const entry of entries) {
    const full = join(dir, entry)
    let isDir: boolean
    try {
      isDir = statSync(full).isDirectory()
    } catch {
      continue
    }
    if (isDir) {
      if (hasAnyFileSync(full)) return true
    } else {
      return true
    }
  }
  return false
}

/** 运行一次 worker agent turn 的抽象（返回 PlanDecision 或 null）。 */
export interface AgentTurn {
  turn(prompt: string): Promise<PlanDecision | null>
}

/** 运行 evaluator agent 直到冻结出一个 evaluator bundle。 */
export async function runEvaluatorPlan(opts: {
  agent: AgentTurn
  scripts: DataScriptRunner
  store: ArtifactStore
  evaluatorDir: string
  task: string
  maxTurns: number
}): Promise<ArtifactRef> {
  if (opts.maxTurns < 1) throw new Error("max_turns must be at least 1")
  let feedback: string | null = null
  for (let turn = 0; turn < opts.maxTurns; turn++) {
    const prompt = feedback ?? opts.task
    const decision = await opts.agent.turn(prompt)
    if (decision === null) {
      feedback = "previous evaluator turn did not produce a valid decision"
      continue
    }
    if (decision.decision === "abandon") {
      throw new Error(`evaluator Agent abandoned Plan: ${decision.reason}`)
    }
    try {
      const evaluatorRef = await freezeEvaluator({
        root: opts.evaluatorDir,
        scripts: opts.scripts,
        store: opts.store,
      })
      if (decision.decision !== "submit") {
        throw new Error(`evaluator frozen successfully but the decision was ${decision.decision}`)
      }
      return evaluatorRef
    } catch (exc) {
      feedback = (exc as Error).message
    }
  }
  throw new Error("evaluator turn budget exhausted without a frozen evaluator")
}

/** 运行 prepare agent 直到产生可信 baseline。 */
export async function runPreparePlan(opts: {
  agent: AgentTurn
  evaluator: Scorer
  git: GitWorkspace
  workspace: GitWorkBranch
  execution: ExecutionRuntime
  store: ArtifactStore
  evaluatorRef: ArtifactRef
  treeRef: ArtifactRef
  task: string
  maxTurns: number
  planRunnerFactory?: (
    branch: GitWorkBranch,
    context: { project_root: string; workspace_root: string; environment_root: string; experiment_id: string }
  ) => { runTurn(planId: string, state: PlanState, planInput: ReturnType<typeof PlanInputSchema.parse>): Promise<PlanTurnResult> }
}): Promise<PrepareResult> {
  if (opts.maxTurns < 1) throw new Error("max_turns must be at least 1")
  const contextRef = await opts.store.putText(
    JSON.stringify({ plan_id: PREPARE_PLAN_ID, task: opts.task, tree_ref: opts.treeRef })
  )
  const makeRunner =
    opts.planRunnerFactory ??
    ((branch, context) =>
      new PlanRunner(opts.execution, opts.store, opts.evaluator, opts.git, branch, context))

  let feedback: string | null = null
  for (let turn = 0; turn < opts.maxTurns; turn++) {
    const prompt = feedback ?? opts.task
    const decision = await opts.agent.turn(prompt)
    if (decision === null) {
      feedback = "previous Agent turn did not produce a valid decision"
      continue
    }
    if (decision.decision === "abandon") {
      throw new Error(`prepare Agent abandoned Plan: ${decision.reason}`)
    }
    try {
      const runner = makeRunner(opts.workspace, {
        project_root: opts.execution.project_root,
        workspace_root: opts.workspace.path,
        environment_root: opts.execution.environment_root,
        experiment_id: PREPARE_PLAN_ID,
      })
      const state = PlanStateSchema.parse({
        kind: "PREPARE",
        context_ref: contextRef,
        turns_used: turn,
        turn_limit: opts.maxTurns,
      })
      const planInput = PlanInputSchema.parse({
        evaluator_ref: opts.evaluatorRef,
        tree_ref: opts.treeRef,
        initial_turn_limit: opts.maxTurns,
      })
      const outcome = await runner.runTurn(PREPARE_PLAN_ID, state, planInput)
      if (outcome.kind === "evaluator_infrastructure_failed") {
        throw new Error(outcome.error ?? "evaluator unavailable")
      }
      if (outcome.kind !== "scored") {
        throw new Error(outcome.error ?? `PREPARE validation failed: ${outcome.kind}`)
      }
      if (outcome.commit === null || outcome.predictions_ref === null || outcome.evidence_ref === null || outcome.report_ref === null || outcome.metric === null) {
        throw new Error("trusted baseline is missing required outputs")
      }
      if (decision.decision !== "submit") {
        throw new Error(`baseline validated successfully but the decision was ${decision.decision}`)
      }
      return PrepareResultSchema.parse({
        evaluator_ref: opts.evaluatorRef,
        metric: outcome.metric,
        commit: outcome.commit,
        predictions_ref: outcome.predictions_ref,
        evidence_ref: outcome.evidence_ref,
        report_ref: outcome.report_ref,
      })
    } catch (exc) {
      if (exc instanceof Error && (exc.message.includes("evaluator unavailable") || exc.message.includes("trusted baseline is missing"))) {
        throw exc
      }
      feedback = (exc as Error).message
    }
  }
  throw new Error("prepare turn budget exhausted without a trusted baseline")
}
