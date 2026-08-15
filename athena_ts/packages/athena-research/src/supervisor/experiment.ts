/**
 * Manifest 执行、可信打分与 patience（移植 ``research/supervisor/experiment.py``）。
 */

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs"
import { join } from "node:path"
import { z } from "zod"
import {
  CommitHash,
  LooseFloat,
  type ArtifactRef,
  type ArtifactStore,
  type GitDiff,
  type GitWorkBranch,
  type GitWorkspace,
} from "@athena/core"
import type { EmitEvent } from "@athena/agent"

import { DataScriptBundleSchema, type DataScriptBundle } from "../contracts.js"
import { loadDirectory, packDirectory } from "../script_runner.js"
import { ScoringError, type Scorer } from "../evaluation.js"
import type { ExecutionContext, ExecutionRuntime } from "../execution.js"
import { PlanBestSchema, PlanStateSchema, type PlanInput, type PlanState } from "./plans.js"
import { redact } from "./events.js"

export type Direction = "maximize" | "minimize"

const FORBIDDEN_EXECUTABLES = new Set(["git", "git.exe"])

function validateRelativePath(path: string, label: string): void {
  if (path.startsWith("/") || /^[A-Za-z]:[\\/]/.test(path)) {
    throw new Error(`${label} path must be relative to the workspace`)
  }
  if (/^[A-Za-z]:/.test(path)) {
    throw new Error(`${label} path must be relative to the workspace`)
  }
  const parts = path.replace(/\\/g, "/").split("/")
  if (parts.some((part) => part === "" || part === "." || part === "..")) {
    throw new Error(`${label} path escapes the workspace`)
  }
}

export interface ExperimentManifest {
  version: number
  commands: string[][]
  outputs: Record<string, string>
}

/** 解析并校验 workspace 根 experiment.json；非法时抛出带字段名的摘要。 */
export function readExperimentManifest(root: string): ExperimentManifest {
  const path = join(root, "experiment.json")
  if (!existsSync(path)) {
    throw new Error("experiment.json is missing")
  }
  let data: unknown
  try {
    data = JSON.parse(readFileSync(path, "utf-8"))
  } catch {
    throw new Error("experiment.json is not valid JSON")
  }
  const obj =
    typeof data === "object" && data !== null && !Array.isArray(data)
      ? (data as Record<string, unknown>)
      : {}
  const errors: string[] = []

  if (obj["version"] !== 1) {
    errors.push("version: only manifest version 1 is supported")
  }

  let commands: string[][] = []
  const rawCommands = obj["commands"]
  if (!Array.isArray(rawCommands)) {
    errors.push("commands: Input should be a valid array")
  } else {
    let normalized: unknown[] = rawCommands
    if (normalized.length > 0 && typeof normalized[0] === "string") {
      normalized = [normalized]
    }
    for (const rawArgv of normalized) {
      if (
        !Array.isArray(rawArgv) ||
        rawArgv.length === 0 ||
        rawArgv.some((part) => typeof part !== "string" || !part.trim())
      ) {
        errors.push("commands: each command must be a non-empty argv array")
        break
      }
      const argv = rawArgv as string[]
      const basename = argv[0]!.replace(/\\/g, "/").split("/").pop()!.toLowerCase()
      if (FORBIDDEN_EXECUTABLES.has(basename)) {
        errors.push("commands: manifest cannot run git commands")
        break
      }
      commands.push(argv)
    }
  }

  let outputs: Record<string, string> = {}
  const rawOutputs = obj["outputs"]
  if (typeof rawOutputs !== "object" || rawOutputs === null || Array.isArray(rawOutputs)) {
    errors.push("outputs: Input should be a valid object")
  } else {
    const out = rawOutputs as Record<string, unknown>
    if (!("predictions" in out)) {
      errors.push("outputs: predictions output is mandatory")
    }
    for (const [key, rel] of Object.entries(out)) {
      if (typeof rel !== "string") {
        errors.push(`outputs: output ${key} must be a string`)
        continue
      }
      try {
        validateRelativePath(rel, `output ${key}`)
        outputs[key] = rel
      } catch (exc) {
        errors.push((exc as Error).message)
      }
    }
  }

  if (errors.length) {
    throw new Error(errors.join("; ").slice(0, 1000))
  }
  return { version: 1, commands, outputs }
}

/** 一次 turn 的终态动作。 */
export interface PlanSettlement {
  action: "settle" | "wait" | "continue"
  best_ref: ArtifactRef | null
  reason: string
}

export const PlanTurnResultSchema = z
  .strictObject({
    kind: z.enum([
      "scored",
      "scoring_failed",
      "evaluator_infrastructure_failed",
      "output_failed",
      "execution_failed",
      "manifest_invalid",
    ]),
    metric: LooseFloat.nullable().default(null),
    commit: CommitHash.nullable().default(null),
    next_state: PlanStateSchema.nullable().default(null),
    predictions_ref: z.string().nullable().default(null),
    evidence_ref: z.string().nullable().default(null),
    report_ref: z.string().nullable().default(null),
    error: z.string().nullable().default(null),
  })
  .superRefine((result, ctx) => {
    if (result.kind === "scored" && result.next_state === null) {
      ctx.addIssue({ code: "custom", message: "scored result requires next_state" })
    }
    if (result.kind !== "scored" && result.next_state !== null) {
      ctx.addIssue({ code: "custom", message: "failure result must not carry next_state" })
    }
  })

export type PlanTurnResult = z.infer<typeof PlanTurnResultSchema>

function better(candidate: number, reference: number, direction: Direction): boolean {
  return direction === "maximize" ? candidate > reference : candidate < reference
}

/** 从 artifact 引用加载不可变 PlanBest 记录。 */
export async function loadBest(bestRef: ArtifactRef, store: ArtifactStore) {
  return PlanBestSchema.parse(JSON.parse(await store.getText(bestRef)))
}

/** 应用一次可信分数：更新 best 并调整 stale_rounds。 */
export async function applyTrustedScore(
  state: PlanState,
  metric: number,
  commit: string,
  opts: {
    store: ArtifactStore
    evidenceRef: ArtifactRef
    direction?: Direction
  }
): Promise<PlanState> {
  const current = state.best_ref ? await loadBest(state.best_ref, opts.store) : null
  const improved = current === null || better(metric, current.metric, opts.direction ?? "maximize")
  if (improved) {
    const best = PlanBestSchema.parse({ metric, commit, evidence_ref: opts.evidenceRef })
    const bestRef = await opts.store.putText(JSON.stringify(best))
    return PlanStateSchema.parse({ ...state, best_ref: bestRef, stale_rounds: 0 })
  }
  return PlanStateSchema.parse({ ...state, stale_rounds: (state.stale_rounds ?? 0) + 1 })
}

/** 根据 Plan 预算与 Agent 决策返回 turn 的终态动作。 */
export function decideSettlement(
  state: PlanState,
  decision: { decision: "continue" | "submit" | "abandon" },
  reportRef: ArtifactRef | null = null
): PlanSettlement {
  const hasBest = state.best_ref !== undefined && state.best_ref !== null
  const patienceExhausted =
    state.patience !== undefined && state.patience !== null && (state.stale_rounds ?? 0) >= state.patience
  const turnsExhausted = state.turn_limit !== null && state.turns_used >= state.turn_limit

  let settlement: PlanSettlement
  if (decision.decision === "submit" || decision.decision === "abandon") {
    settlement = { action: "settle", best_ref: state.best_ref ?? null, reason: decision.decision }
  } else if (patienceExhausted) {
    settlement = { action: "settle", best_ref: state.best_ref ?? null, reason: "patience exhausted" }
  } else if (turnsExhausted) {
    settlement = hasBest
      ? { action: "settle", best_ref: state.best_ref ?? null, reason: "turn budget exhausted" }
      : { action: "wait", best_ref: null, reason: "turn budget exhausted without a trusted best" }
  } else {
    settlement = { action: "continue", best_ref: null, reason: "" }
  }

  if (settlement.action === "settle" && state.kind === "PREPARE" && reportRef === null) {
    return { action: "wait", best_ref: null, reason: "report required before settlement" }
  }
  return settlement
}

function cleanError(error: string): string {
  return redact(error.split(/\s+/).join(" ")).slice(0, 1000)
}

/** 执行一次 Plan turn：manifest、打分、提交与 trusted patience。 */
export class PlanRunner {
  constructor(
    private execution: ExecutionRuntime,
    private store: ArtifactStore,
    private evaluator: Scorer,
    private workspace: GitWorkspace,
    private branch: GitWorkBranch,
    private context: ExecutionContext,
    private direction: Direction = "maximize",
    private timeoutS: number = 120
  ) {}

  get workdir(): string {
    return this.branch.path
  }

  async runTurn(
    planId: string,
    state: PlanState,
    planInput: PlanInput,
    emit: EmitEvent | null = null
  ): Promise<PlanTurnResult> {
    let manifest: ExperimentManifest
    try {
      manifest = readExperimentManifest(this.workdir)
    } catch (exc) {
      return this.failure(planId, "manifest_invalid", cleanError((exc as Error).message))
    }

    for (const argv of manifest.commands) {
      const result = await this.execution.run(this.context, {
        argv,
        timeout_s: this.timeoutS,
        workdir: this.workdir,
        emit,
      })
      if (!result.ok) {
        let error = `command failed (exit ${result.exit_code}): ${result.stderr.slice(0, 200)}`
        if (result.stderr.includes("ModuleNotFoundError")) {
          error += ' Run "uv sync --project $ATHENA_ENV_ROOT" to install the declared dependencies into the environment venv, then retry.'
        }
        return this.failure(planId, "execution_failed", cleanError(error))
      }
    }

    const predictionsRoot = manifest.outputs["predictions"]!
    const predictionsDir = join(this.workdir, predictionsRoot)
    if (!existsSync(predictionsDir) || !hasAnyFile(predictionsDir)) {
      return this.failure(planId, "output_failed", `missing predictions directory: ${predictionsRoot}`)
    }
    const predictionsRef = await packDirectory(this.store, predictionsDir)
    const predictions = await loadDirectory(this.store, predictionsRef)
    const reportRef = await this.storeReport(manifest)
    if (state.kind === "PREPARE" && reportRef === null) {
      return this.failure(
        planId,
        "output_failed",
        "PREPARE requires a declared, non-empty report output",
        predictionsRef
      )
    }

    const bundle = await this.loadBundle(planInput.evaluator_ref)
    if (bundle === null) {
      return this.failure(planId, "scoring_failed", "frozen evaluator artifact is invalid", predictionsRef)
    }
    let evaluation
    try {
      evaluation = await this.evaluator.score({
        evalBundle: bundle,
        predictions,
        candidateId: planId,
        direction: this.direction,
        predictionsRoot,
      })
    } catch (exc) {
      if (exc instanceof ScoringError) {
        return this.failure(planId, "scoring_failed", cleanError(exc.message), predictionsRef)
      }
      return this.failure(
        planId,
        "evaluator_infrastructure_failed",
        cleanError((exc as Error).message),
        predictionsRef
      )
    }
    const metric = evaluation.test_score

    const diff: GitDiff = await this.workspace.diff(this.branch)
    const commit = await this.workspace.commit(
      this.branch,
      diff,
      `plan ${planId} trusted score ${metric.toFixed(4)}`
    )
    const evidenceRef = await this.store.putText(
      JSON.stringify({
        plan: planId,
        metric,
        commit,
        predictions_ref: predictionsRef,
        report_ref: reportRef,
        outputs: manifest.outputs,
      })
    )
    let updated = state
    if (state.kind === "SEARCH") {
      updated = await applyTrustedScore(state, metric, commit, {
        store: this.store,
        evidenceRef,
        direction: this.direction,
      })
    }
    return PlanTurnResultSchema.parse({
      kind: "scored",
      metric,
      commit,
      next_state: updated,
      predictions_ref: predictionsRef,
      evidence_ref: evidenceRef,
      report_ref: reportRef,
    })
  }

  private async failure(
    planId: string,
    kind: "scoring_failed" | "evaluator_infrastructure_failed" | "output_failed" | "execution_failed" | "manifest_invalid",
    error: string,
    predictionsRef: ArtifactRef | null = null
  ): Promise<PlanTurnResult> {
    const cleaned = cleanError(error)
    const evidenceRef = await this.store.putText(
      JSON.stringify({ plan: planId, kind, error: cleaned, predictions_ref: predictionsRef })
    )
    return PlanTurnResultSchema.parse({
      kind,
      predictions_ref: predictionsRef,
      evidence_ref: evidenceRef,
      error: cleaned,
    })
  }

  private async loadBundle(evaluatorRef: ArtifactRef): Promise<DataScriptBundle | null> {
    let text: string
    try {
      text = await this.store.getText(evaluatorRef)
    } catch {
      return null
    }
    try {
      return DataScriptBundleSchema.parse(JSON.parse(text))
    } catch {
      return null
    }
  }

  private async storeReport(manifest: ExperimentManifest): Promise<ArtifactRef | null> {
    const report = manifest.outputs["report"]
    if (report === undefined) return null
    const reportPath = join(this.workdir, report)
    if (!existsSync(reportPath)) return null
    const content = readFileSync(reportPath, "utf-8")
    if (!content) return null
    return this.store.putText(content)
  }
}

/** 目录（递归）内是否存在至少一个文件。 */
function hasAnyFile(dir: string): boolean {
  let entries: string[]
  try {
    entries = readdirSync(dir)
  } catch {
    return false
  }
  for (const entry of entries) {
    const full = join(dir, entry)
    let isDirectory: boolean
    try {
      isDirectory = statSync(full).isDirectory()
    } catch {
      continue
    }
    if (isDirectory) {
      if (hasAnyFile(full)) return true
    } else {
      return true
    }
  }
  return false
}
