import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import {
  GitWorkBranchSchema,
  LocalArtifactStore,
  parseOrThrow,
  type GitDiff,
  type GitWorkBranch,
} from "@athena/core"
import { DataScriptBundleSchema, type DataScriptBundle } from "../../src/contracts.js"
import { ScoringError } from "../../src/evaluation.js"
import { CommandResult } from "../../src/execution.js"
import { loadDirectory } from "../../src/script_runner.js"
import {
  PlanRunner,
  PlanTurnResultSchema,
  applyTrustedScore,
  loadBest,
  hasAnyFile,
  readExperimentManifest,
} from "../../src/supervisor/experiment.js"
import { PlanBestSchema, PlanInputSchema, PlanStateSchema } from "../../src/contracts.js"

const REF = "sha256:" + "a".repeat(64)
const OTHER_REF = "sha256:" + "c".repeat(64)


function searcState(opts: { staleRounds?: number; bestRef?: string | null } = {}) {
  const payload: Record<string, unknown> = {
    kind: "SEARCH",
    context_ref: REF,
    turns_used: 2,
    turn_limit: 12,
    patience: 4,
  }
  if (opts.staleRounds !== undefined) payload["stale_rounds"] = opts.staleRounds
  if (opts.bestRef !== undefined) payload["best_ref"] = opts.bestRef
  return parseOrThrow(PlanStateSchema, payload)
}

let tmp: string
afterEach(() => {
  if (tmp) rmSync(tmp, { recursive: true, force: true })
})

function tmpDir(): string {
  tmp = mkdtempSync(join(tmpdir(), "athena-exp-"))
  return tmp
}

describe("PlanTurnResult", () => {
  it("scored turn result requires next_state", () => {
    expect(() => parseOrThrow(PlanTurnResultSchema, { kind: "scored" })).toThrow(/next_state/)
  })

  it("failed turn result rejects state payload", () => {
    const state = parseOrThrow(PlanStateSchema, { kind: "PREPARE", context_ref: REF, turns_used: 0, turn_limit: 12 })
    expect(() => parseOrThrow(PlanTurnResultSchema, { kind: "execution_failed", error: "boom", next_state: state })).toThrow(/next_state/)
    expect(() => parseOrThrow(PlanTurnResultSchema, { kind: "execution_failed", error: "boom", stale_rounds: 0 })).toThrow()
    expect(() => parseOrThrow(PlanTurnResultSchema, { kind: "execution_failed", error: "boom", best_ref: OTHER_REF })).toThrow()
  })
})

describe("ExperimentManifest", () => {
  it("rejects shell strings and path escape", () => {
    expect(() =>
      readExperimentManifest(writeManifest({ commands: ["python train.py"], outputs: { predictions: "../labels.csv", report: "report.md" } }))
    ).toThrow()
  })

  it("rejects shell string command", () => {
    expect(() =>
      readExperimentManifest(writeManifest({ commands: [["python", "train.py"], "uv run predict.py"], outputs: { predictions: "outputs/predictions.csv" } }))
    ).toThrow()
  })

  it("rejects escaping output path", () => {
    expect(() =>
      readExperimentManifest(writeManifest({ commands: [["python", "train.py"]], outputs: { predictions: "../labels.csv" } }))
    ).toThrow(/escapes/)
  })

  it("rejects absolute output path", () => {
    expect(() =>
      readExperimentManifest(writeManifest({ commands: [["python", "train.py"]], outputs: { predictions: "/tmp/abs.csv" } }))
    ).toThrow()
  })

  it("rejects git command", () => {
    expect(() =>
      readExperimentManifest(writeManifest({ commands: [["git", "status"]], outputs: { predictions: "outputs/predictions.csv" } }))
    ).toThrow(/git/)
  })

  it("rejects unsupported version", () => {
    expect(() =>
      readExperimentManifest(writeManifest({ version: 2, commands: [["python", "train.py"]], outputs: { predictions: "outputs/predictions.csv" } }))
    ).toThrow(/version/)
  })

  it("requires predictions output", () => {
    expect(() =>
      readExperimentManifest(writeManifest({ commands: [["python", "train.py"]], outputs: { report: "report.md" } }))
    ).toThrow(/predictions/)
  })

  it("rejects empty command", () => {
    expect(() =>
      readExperimentManifest(writeManifest({ commands: [[]], outputs: { predictions: "outputs/predictions.csv" } }))
    ).toThrow()
  })

  it("accepts argv manifest", () => {
    const manifest = readExperimentManifest(
      writeManifest({
        version: 1,
        commands: [["uv", "run", "python", "-m", "solution.train"]],
        outputs: { predictions: "outputs/predictions.csv", report: "outputs/report.md" },
      })
    )
    expect(manifest.commands[0]).toEqual(["uv", "run", "python", "-m", "solution.train"])
    expect(manifest.outputs["predictions"]).toBe("outputs/predictions.csv")
  })
})

describe("applyTrustedScore", () => {
  it("trusted improvement resets patience and keeps historical best", async () => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    const state = await scoredPlan(store, { bestMetric: 0.8, staleRounds: 2, patience: 3 })

    const improved = await applyTrustedScore(state, 0.82, "c2", { store, evidenceRef: OTHER_REF })
    expect(improved.stale_rounds).toBe(0)

    const worse = await applyTrustedScore(improved, 0.81, "c3", { store, evidenceRef: OTHER_REF })
    expect(worse.stale_rounds).toBe(1)

    const best = await loadBest(worse.best_ref!, store)
    expect(best.commit).toBe("c2")
    expect(best.metric).toBe(0.82)
  })

  it("first trusted score sets best and zeroes stale", async () => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    const state = parseOrThrow(PlanStateSchema, {
      kind: "SEARCH",
      context_ref: REF,
      turns_used: 1,
      turn_limit: 12,
      patience: 3,
    })

    const scored = await applyTrustedScore(state, 0.84, "c1", { store, evidenceRef: OTHER_REF })
    expect(scored.stale_rounds).toBe(0)
    expect(scored.best_ref).not.toBeNull()
    const best = await loadBest(scored.best_ref!, store)
    expect(best.commit).toBe("c1")
    expect(best.metric).toBe(0.84)
  })

  it("minimize direction counts lower as improvement", async () => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    const state = await scoredPlan(store, { bestMetric: 0.3, staleRounds: 1, patience: 3 })

    const improved = await applyTrustedScore(state, 0.25, "c2", { store, evidenceRef: OTHER_REF, direction: "minimize" })
    expect(improved.stale_rounds).toBe(0)

    const worse = await applyTrustedScore(improved, 0.27, "c3", { store, evidenceRef: OTHER_REF, direction: "minimize" })
    expect(worse.stale_rounds).toBe(1)
    expect((await loadBest(worse.best_ref!, store)).commit).toBe("c2")
  })
})


async function scoredPlan(store: LocalArtifactStore, opts: { bestMetric: number; staleRounds: number; patience: number }) {
  const best = PlanBestSchema.parse({ metric: opts.bestMetric, commit: "c1", evidence_ref: OTHER_REF })
  const bestRef = await store.putText(JSON.stringify(best))
  return parseOrThrow(PlanStateSchema, {
    kind: "SEARCH",
    context_ref: REF,
    turns_used: 1,
    turn_limit: 12,
    patience: opts.patience,
    stale_rounds: opts.staleRounds,
    best_ref: bestRef,
  })
}

function writeManifest(payload: unknown): string {
  const dir = tmpDir()
  writeFileSync(join(dir, "experiment.json"), JSON.stringify(payload), "utf-8")
  return dir
}

// ── PlanRunner.run_turn 集成测试 ─────────────────────────────

class FakeExecution {
  constructor(private results: CommandResult[]) {}
  calls: string[][] = []
  workdirs: Array<string | null> = []
  emitSeen: unknown[] = []
  async run(
    opts: { argv?: string[] | null; timeout_s?: number; workdir?: string | null; emit?: unknown }
  ): Promise<CommandResult> {
    this.calls.push(opts.argv ?? [])
    expect(opts.timeout_s).toBe(120)
    this.workdirs.push(opts.workdir ?? null)
    if (opts.emit) {
      ;(opts.emit as (k: string, r: string, d: unknown) => void)("command/started", "exec:run", { command: opts.argv })
      this.emitSeen.push(opts.emit)
    }
    return this.results.shift()!
  }
}

class FakeWorkspace {
  constructor(private commits: string[]) {}
  diffs: GitDiff[] = []
  messages: string[] = []
  async diff(_workspace: GitWorkBranch): Promise<GitDiff> {
    const diff: GitDiff = { ref: OTHER_REF, paths: ["experiment.json"] }
    this.diffs.push(diff)
    return diff
  }
  async commit(_workspace: GitWorkBranch, _diff: GitDiff, message: string): Promise<string> {
    this.messages.push(message)
    return this.commits.shift()!
  }
}

class FakeEvaluator {
  constructor(
    private metric = 0.9,
    private error: Error | null = null
  ) {}
  async score() {
    if (this.error) throw this.error
    return this.metric
  }
}

async function runnerSetup(execution: FakeExecution, evaluator: FakeEvaluator) {
  const dir = tmpDir()
  const store = new LocalArtifactStore(join(dir, "artifacts"))
  const bundle = DataScriptBundleSchema.parse({ bundle_id: "b1", entrypoint: "eval.py" })
  const evaluatorRef = await store.putText(JSON.stringify(bundle))
  const planInput = parseOrThrow(PlanInputSchema, { evaluator_ref: evaluatorRef, tree_ref: OTHER_REF })
  const branch = GitWorkBranchSchema.parse({
    path: join(dir, "ws"),
    branch: "athena/plan/h1",
    base_commit: "c0",
  })
  mkdirSync(branch.path, { recursive: true })
  const workspace = new FakeWorkspace(["c1"])
  const runner = new PlanRunner(
    execution as never,
    store,
    evaluator as never,
    workspace as never,
    branch
  )
  return { runner, planInput, workspace, branch, store }
}

function writeRunManifest(branch: GitWorkBranch, opts: { commands: string[][]; outputs?: Record<string, string>; predictions?: string } = {}) {
  const workdir = branch.path
  const outputs = opts.outputs ?? { predictions: "outputs/predictions", report: "report.md" }
  writeFileSync(join(workdir, "experiment.json"), JSON.stringify({ version: 1, commands: opts.commands, outputs }), "utf-8")
  const predictionsDir = join(workdir, outputs["predictions"]!)
  mkdirSync(predictionsDir, { recursive: true })
  writeFileSync(join(predictionsDir, "predictions.csv"), opts.predictions ?? "id,pred\n")
  writeFileSync(join(workdir, "report.md"), "# report\n", "utf-8")
}

describe("PlanRunner.run_turn", () => {
  it("checks nested directory contents without requiring nonempty files", () => {
    const dir = tmpDir()
    expect(hasAnyFile(join(dir, "missing"))).toBe(false)
    expect(hasAnyFile(dir)).toBe(false)
    mkdirSync(join(dir, "nested", "empty"), { recursive: true })
    expect(hasAnyFile(dir)).toBe(false)
    writeFileSync(join(dir, "nested", "empty", "data.csv"), "")
    expect(hasAnyFile(dir)).toBe(true)
    expect(hasAnyFile(join(dir, "nested", "empty", "data.csv"))).toBe(false)
  })

  it.each([null, "not json", "{}"])("normalizes missing or invalid evaluator bundles (%j)", async (payload) => {
    const { runner, planInput, branch, workspace, store } = await runnerSetup(new FakeExecution([]), new FakeEvaluator())
    writeRunManifest(branch, { commands: [] })
    const input = { ...planInput, evaluator_ref: payload === null ? OTHER_REF : await store.putText(payload) }
    const result = await runner.runTurn("h1", searcState(), input)
    expect(result.kind).toBe("scoring_failed")
    expect(result.error).toBe("frozen evaluator artifact is invalid")
    expect(result.next_state).toBeNull()
    expect(workspace.messages).toEqual([])
    expect(JSON.parse(await store.getText(result.evidence_ref!)).error).toBe(result.error)
  })

  it("executes manifest, scores and commits", async () => {
    const execution = new FakeExecution([new CommandResult(true, "ok", "", 0)])
    const { runner, planInput, workspace, branch, store } = await runnerSetup(execution, new FakeEvaluator(0.91))
    writeRunManifest(branch, { commands: [["node", "-e", "console.log('train')"]], predictions: "__athena_row_id,prediction\nrow_1,1\nrow_2,0\n" })
    const state = parseOrThrow(PlanStateSchema, { kind: "SEARCH", context_ref: REF, turns_used: 1, turn_limit: 12, patience: 4 })

    const result = await runner.runTurn("h1", state, planInput)

    expect(result.kind).toBe("scored")
    expect(result.metric).toBe(0.91)
    expect(result.commit).toBe("c1")
    expect(result.next_state).not.toBeNull()
    expect(result.next_state!.stale_rounds).toBe(0)
    expect(result.next_state!.best_ref).not.toBeNull()
    const best = await loadBest(result.next_state!.best_ref!, store)
    expect(best.metric).toBe(0.91)
    expect(workspace.messages).toEqual(["plan h1 trusted score 0.9100"])
    expect(execution.calls).toEqual([["node", "-e", "console.log('train')"]])
    expect(result.predictions_ref).not.toBeNull()
    const tree = await loadDirectory(store, result.predictions_ref!)
    expect(tree).toEqual({ "predictions.csv": Buffer.from("__athena_row_id,prediction\nrow_1,1\nrow_2,0\n") })
  })

  it("execution failure does not increment stale", async () => {
    const execution = new FakeExecution([new CommandResult(false, "", "boom", 1)])
    const { runner, planInput, workspace, branch } = await runnerSetup(execution, new FakeEvaluator(0.91))
    writeRunManifest(branch, { commands: [["node", "train.py"]] })
    const state = parseOrThrow(PlanStateSchema, { kind: "SEARCH", context_ref: REF, turns_used: 1, turn_limit: 12, patience: 4, stale_rounds: 2 })

    const result = await runner.runTurn("h1", state, planInput)

    expect(result.kind).toBe("execution_failed")
    expect(result.next_state).toBeNull()
    expect(workspace.messages).toEqual([])
    expect(state.stale_rounds).toBe(2)
  })

  it("scoring failure does not increment stale", async () => {
    const execution = new FakeExecution([new CommandResult(true, "ok", "", 0)])
    const { runner, planInput, workspace, branch, store } = await runnerSetup(
      execution,
      new FakeEvaluator(0.9, new ScoringError("row ids do not align"))
    )
    writeRunManifest(branch, { commands: [["node", "predict.py"]] })
    const state = parseOrThrow(PlanStateSchema, { kind: "SEARCH", context_ref: REF, turns_used: 1, turn_limit: 12, patience: 4, stale_rounds: 2 })

    const result = await runner.runTurn("h1", state, planInput)

    expect(result.kind).toBe("scoring_failed")
    expect(result.next_state).toBeNull()
    expect(workspace.messages).toEqual([])
    expect(state.stale_rounds).toBe(2)
    const tree = await loadDirectory(store, result.predictions_ref!)
    expect(tree).toEqual({ "predictions.csv": Buffer.from("id,pred\n") })
  })

  it("evaluator infrastructure failure is retryable", async () => {
    const execution = new FakeExecution([new CommandResult(true, "ok", "", 0)])
    const { runner, planInput, workspace, branch } = await runnerSetup(
      execution,
      new FakeEvaluator(0.9, new Error("provider unavailable"))
    )
    writeRunManifest(branch, { commands: [["node", "predict.py"]] })
    const state = parseOrThrow(PlanStateSchema, { kind: "SEARCH", context_ref: REF, turns_used: 1, turn_limit: 12, patience: 4, stale_rounds: 2 })

    const result = await runner.runTurn("h1", state, planInput)

    expect(result.kind).toBe("evaluator_infrastructure_failed")
    expect(result.next_state).toBeNull()
    expect(workspace.messages).toEqual([])
  })

  it("redacts evaluator failure details", async () => {
    const execution = new FakeExecution([new CommandResult(true, "ok", "", 0)])
    const { runner, planInput, branch, store } = await runnerSetup(
      execution,
      new FakeEvaluator(0.9, new ScoringError("row ids do not align; api_key=private-value"))
    )
    writeRunManifest(branch, { commands: [["node", "predict.py"]] })
    const state = parseOrThrow(PlanStateSchema, { kind: "SEARCH", context_ref: REF, turns_used: 1, turn_limit: 12, patience: 4, stale_rounds: 2 })

    const result = await runner.runTurn("h1", state, planInput)

    expect(result.kind).toBe("scoring_failed")
    expect(result.error).not.toContain("private-value")
    expect(result.error).toContain("[REDACTED]")
    const evidence = JSON.parse(await store.getText(result.evidence_ref!))
    expect(evidence["error"]).not.toContain("private-value")
    expect(evidence["error"]).toContain("[REDACTED]")
  })

  it("invalid manifest is rejected without running commands", async () => {
    const execution = new FakeExecution([new CommandResult(true, "ok", "", 0)])
    const { runner, planInput, workspace, branch, store } = await runnerSetup(execution, new FakeEvaluator())
    const secretCommand = "private-command-token"
    writeFileSync(join(branch.path, "experiment.json"), JSON.stringify({ version: 1, commands: secretCommand, outputs: { predictions: "outputs/predictions.csv" } }), "utf-8")
    const state = parseOrThrow(PlanStateSchema, { kind: "SEARCH", context_ref: REF, turns_used: 1, turn_limit: 12, patience: 4 })

    const result = await runner.runTurn("h1", state, planInput)

    expect(result.kind).toBe("manifest_invalid")
    expect(result.error).toContain("commands")
    expect(result.error).toContain("valid array")
    expect(result.error).not.toContain(secretCommand)
    expect(execution.calls).toEqual([])
    expect(workspace.messages).toEqual([])
    const evidence = JSON.parse(await store.getText(result.evidence_ref!))
    expect(evidence["kind"]).toBe("manifest_invalid")
    expect(evidence["error"]).toBe(result.error)
  })
})
