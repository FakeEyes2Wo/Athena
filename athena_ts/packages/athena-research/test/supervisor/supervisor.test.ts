import { mkdirSync, mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import {
  ExperimentPlanSchema,
  ExperimentSchema,
  HypothesisSchema,
  LocalArtifactStore,
  ResearchTree,
  type ArtifactRef,
  type GitWorkBranch,
  type GitWorkspace,
} from "@athena/core"
import { ValidationResultSchema } from "../../src/contracts.js"
import { PlanTurnResultSchema } from "../../src/supervisor/experiment.js"
import { PlanBestSchema, PlanDecisionSchema, PlanStateSchema } from "../../src/contracts.js"
import { PrepareResultSchema } from "../../src/supervisor/prepare.js"
import { parseResearchState, loadResearchState, researchStateToJSON } from "../../src/supervisor/state.js"
import { FixedFlowSupervisor, type SupervisorWorkers } from "../../src/supervisor/supervisor.js"

const REF = "sha256:" + "a".repeat(64)

let tmp: string
afterEach(() => {
  if (tmp) rmSync(tmp, { recursive: true, force: true })
})

function tmpDir(): string {
  tmp = mkdtempSync(join(tmpdir(), "athena-sup-"))
  return tmp
}

function baselineTree(dir: string): ResearchTree {
  const tree = new ResearchTree()
  tree.addHypothesis(
    HypothesisSchema.parse({
      id: "baseline",
      statement: "baseline",
      intervention: "baseline",
      expected_effect: "reference",
    })
  )
  tree.addExperiment(
    "exp_baseline",
    ExperimentSchema.parse({
      hypothesis_id: "baseline",
      commit: "c0",
      plan: ExperimentPlanSchema.parse({
        kind: "baseline",
        change: "baseline",
        run_config_ref: REF,
        budget: {},
        acceptance_rule: "trusted score",
      }),
      gitwork: { path: join(dir, "ws"), branch: "main", base_commit: "c0" },
      status: "SUCCEEDED",
      eval: { experiment_id: "exp_baseline", primary: 0.8, per_sample: REF },
    })
  )
  tree.setSota("exp_baseline")
  return tree
}

function fakeGit(root: string): GitWorkspace {
  mkdirSync(root, { recursive: true })
  return {
    async init() {
      return "c0"
    },
    async create(baseCommit: string, branch: string): Promise<GitWorkBranch> {
      mkdirSync(join(root, branch), { recursive: true })
      return { path: join(root, branch), branch, base_commit: baseCommit }
    },
    async diff() {
      return { ref: REF, paths: [] }
    },
    async commit() {
      return "c1"
    },
    async restorePaths() {},
    async remove() {},
  }
}

describe("FixedFlowSupervisor", () => {
  it("runs the fixed SEARCH flow to completion", async () => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    const tree = baselineTree(dir)
    const state = parseResearchState({
      status: "RUNNING",
      phase: "SEARCH",
      search_limit: 2,
      concurrency: 2,
    })

    const workers: SupervisorWorkers = {
      publish: async () => {},
      runPreparePhase: async () =>
        PrepareResultSchema.parse({
          evaluator_ref: REF,
          metric: 0.8,
          commit: "c0",
          predictions_ref: REF,
          evidence_ref: REF,
          report_ref: REF,
        }),
      runValidationPhase: async (_commit, metric) =>
        ValidationResultSchema.parse({
          result_id: "vr_1",
          status: "COMPLETED",
          test_score: metric,
          final_test_score: metric,
        }),
      runIdeatorTurn: async (count) =>
        Array.from({ length: count }, (_, i) =>
          HypothesisSchema.parse({
            statement: `claim ${i}`,
            intervention: `change ${i}`,
            expected_effect: "improve",
          })
        ),
      runPlanAgentTurn: async () => PlanDecisionSchema.parse({ decision: "submit", reason: "done" }),
      runPlanTurn: async (planId, planState) => {
        const best = PlanBestSchema.parse({ metric: 0.85, commit: "c1", evidence_ref: REF })
        const bestRef = await store.putText(JSON.stringify(best))
        return PlanTurnResultSchema.parse({
          kind: "scored",
          metric: 0.85,
          commit: "c1",
          next_state: PlanStateSchema.parse({ ...planState, best_ref: bestRef, stale_rounds: 0 }),
          predictions_ref: REF,
          evidence_ref: REF,
        })
      },
    }

    const supervisor = new FixedFlowSupervisor({
      projectRoot: dir,
      state,
      tree,
      store,
      git: fakeGit(join(dir, "worktrees")),
      evaluatorRef: REF,
      workers,
    })

    await supervisor.start()

    const experiments = tree.experiments("search")
    expect(experiments.length).toBe(2)
    expect(experiments.every((e) => e.status === "SUCCEEDED")).toBe(true)
    expect(tree.bestExperimentId()).not.toBeNull()
    expect(Object.keys(state.plans)).toEqual([])
  })
})

function coreSupervisor(
  dir: string,
  opts: { autoValidate?: boolean; workers?: Partial<SupervisorWorkers> } = {}
) {
  const store = new LocalArtifactStore(join(dir, "artifacts"))
  const tree = baselineTree(dir)
  const state = parseResearchState({
    status: "RUNNING",
    phase: "SEARCH",
    search_limit: 10,
    concurrency: 2,
  })
  const workers: SupervisorWorkers = {
    publish: async () => {},
    runPreparePhase: async () => {
      throw new Error("not used")
    },
    runValidationPhase: async (_commit, metric) =>
      ValidationResultSchema.parse({
        result_id: "vr_1",
        status: "COMPLETED",
        test_score: metric,
        final_test_score: metric,
      }),
    runIdeatorTurn: async () => [],
    runPlanAgentTurn: async () => PlanDecisionSchema.parse({ decision: "continue", reason: "keep going" }),
    runPlanTurn: async () => {
      throw new Error("not used")
    },
    ...opts.workers,
  }
  const supervisor = new FixedFlowSupervisor({
    projectRoot: dir,
    state,
    tree,
    store,
    git: fakeGit(join(dir, "worktrees")),
    evaluatorRef: REF,
    workers,
    autoValidate: opts.autoValidate ?? false,
  })
  return { supervisor, store, state, tree }
}

function runSearch(supervisor: FixedFlowSupervisor): Promise<void> {
  return supervisor["runPhase"](() => supervisor["runSearchLoop"]())
}

describe("FixedFlowSupervisor core actions", () => {
  it("joins SEARCH before starting interactive validation", async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    const runPlanAgentTurn = vi.fn(async () => {
      await gate
      return PlanDecisionSchema.parse({ decision: "abandon", reason: "done" })
    })
    const runValidationPhase = vi.fn(async () => ValidationResultSchema.parse({
      result_id: "validation", status: "COMPLETED", test_score: 0.8, final_test_score: 0.8,
    }))
    const { supervisor, tree } = coreSupervisor(tmpDir(), { autoValidate: true, workers: { runPlanAgentTurn, runValidationPhase } })
    tree.addHypothesis(HypothesisSchema.parse({
      statement: "idea", intervention: "change", expected_effect: "improve", parent_id: "exp_baseline",
    }))
    const run = supervisor.start()
    try {
      await vi.waitFor(() => expect(runPlanAgentTurn).toHaveBeenCalledTimes(1))
      const validation = supervisor.setPhaseDecision("VALIDATE")
      await new Promise<void>((resolve) => setImmediate(resolve))
      expect(runValidationPhase).not.toHaveBeenCalled()
      release()
      await Promise.all([run, validation])
      expect(runValidationPhase).toHaveBeenCalledTimes(1)
      expect(tree.experiments("search")[0]!.status).toBe("FAILED")
      expect(supervisor.state.status).toBe("COMPLETED")
    } finally {
      release()
      await run
      await supervisor.requestStop()
    }
  })

  it.each(["PREPARE", "VALIDATE"] as const)("retains %s failure when stopping its active worker", async (phase) => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    const work = vi.fn(async () => { await gate; throw new Error("phase failed") })
    const { supervisor, state } = coreSupervisor(tmpDir(), { workers: { runPreparePhase: work, runValidationPhase: work } })
    state.phase = phase
    const run = supervisor.start()
    try {
      await vi.waitFor(() => expect(work).toHaveBeenCalledTimes(1))
      const stopping = supervisor.requestStop()
      release()
      await Promise.all([run, stopping])
      expect(supervisor.state.status).toBe("FAILED")
    } finally {
      release()
      await run
      await supervisor.requestStop()
    }
  })

  it.each(["PREPARE", "VALIDATE", "interactive"] as const)("waits for the active %s phase before stop returns", async (phase) => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    const work = vi.fn(async () => { await gate })
    const runIdeatorTurn = vi.fn(async () => [])
    const { supervisor, state } = coreSupervisor(tmpDir(), { workers: {
      runIdeatorTurn,
      runPreparePhase: async () => {
        await work()
        return PrepareResultSchema.parse({ evaluator_ref: REF, metric: 0.8, commit: "c0",
          predictions_ref: REF, evidence_ref: REF, report_ref: REF })
      },
      runValidationPhase: async () => {
        await work()
        return ValidationResultSchema.parse({ result_id: "validation", status: "COMPLETED", test_score: 0.8, final_test_score: 0.8 })
      },
    } })
    state.phase = phase === "interactive" ? "SEARCH" : phase
    const run = phase === "interactive" ? supervisor.setPhaseDecision("VALIDATE") : supervisor.start()
    let stopped = false
    try {
      await vi.waitFor(() => expect(work).toHaveBeenCalledTimes(1))
      const stopping = supervisor.requestStop().then(() => { stopped = true })
      await new Promise<void>((resolve) => setImmediate(resolve))
      expect(stopped).toBe(false)
      release()
      await stopping
      const snapshot = researchStateToJSON(supervisor.state)
      await run
      expect(researchStateToJSON(supervisor.state)).toEqual(snapshot)
      expect(supervisor.state.status).toBe(phase === "PREPARE" ? "STOPPED" : "COMPLETED")
      expect(runIdeatorTurn).not.toHaveBeenCalled()
    } finally {
      release()
      await run
      await supervisor.requestStop()
    }
  })

  it("observes turn rejection while slot filling is still awaiting ideation", async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    const runPlanTurn = vi.fn(async () => { throw new Error("early turn failure") })
    const runIdeatorTurn = vi.fn(async () => { await gate; return [] })
    const { supervisor, tree } = coreSupervisor(tmpDir(), { workers: {
      runPlanTurn, runIdeatorTurn,
      runPlanAgentTurn: async () => PlanDecisionSchema.parse({ decision: "submit", reason: "done" }),
    } })
    tree.addHypothesis(HypothesisSchema.parse({
      id: "bad", statement: "idea", intervention: "change", expected_effect: "improve", parent_id: "exp_baseline",
    }))
    const run = supervisor.start()
    try {
      await vi.waitFor(() => expect(runPlanTurn).toHaveBeenCalledTimes(1))
      expect(runIdeatorTurn).toHaveBeenCalledTimes(1)
      await new Promise<void>((resolve) => setImmediate(resolve))
      release()
      await run
      expect(supervisor.state.status).toBe("FAILED")
      expect(supervisor.runningPlanIds).toEqual([])
      expect(Object.keys(supervisor.state.plans)).toEqual(["bad"])
    } finally {
      release()
      await supervisor.requestStop()
      await run
    }
  })

  it.each(["turn", "ideator", "undefined"] as const)("drains a sibling after a %s rejection", async (source) => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    const publish = vi.fn(async (_kind: string, _data: Record<string, unknown>) => {})
    const failure = source === "undefined" ? undefined : new Error("primary failure")
    const failing = vi.fn(async () => { throw failure })
    const runPlanAgentTurn = vi.fn(async (id: string) => {
      if (id === "good") await gate
      return PlanDecisionSchema.parse({ decision: id === "good" ? "abandon" : "submit", reason: "done" })
    })
    const { supervisor, tree } = coreSupervisor(tmpDir(), { workers: {
      publish, runPlanAgentTurn, runPlanTurn: failing,
      runIdeatorTurn: source === "ideator" ? failing : async () => [],
    } })
    for (const id of source === "ideator" ? ["good"] : ["good", "bad"]) {
      tree.addHypothesis(HypothesisSchema.parse({
        id, statement: id, intervention: "change", expected_effect: "improve", parent_id: "exp_baseline",
      }))
    }
    let finished = false
    const run = supervisor.start().then(() => { finished = true })
    try {
      await vi.waitFor(() => expect(failing).toHaveBeenCalledTimes(1))
      await new Promise<void>((resolve) => setImmediate(resolve))
      expect(finished).toBe(false)
      release()
      await run
      expect(tree.getExperiment("exp_good").status).toBe("FAILED")
      expect(Object.keys(supervisor.state.plans)).toEqual(source === "ideator" ? [] : ["bad"])
      expect(supervisor.runningPlanIds).toEqual([])
      expect(supervisor.state.status).toBe("FAILED")
      expect(publish.mock.calls.filter(([kind]) => kind === "output")).toEqual([
        ["output", { source: "supervisor", channel: "error", text: `research failed: ${failure?.message ?? "undefined"}` }],
      ])
    } finally {
      release()
      await supervisor.requestStop()
      await run
    }
  })

  it.each(["start", "resume"] as const)("persists and reports SEARCH failure through %s", async (entry) => {
    const dir = tmpDir()
    const publish = vi.fn(async (_kind: string, _data: Record<string, unknown>) => {})
    const { supervisor } = coreSupervisor(dir, { workers: {
      publish,
      runIdeatorTurn: async () => { throw new Error("ideation failed") },
    } })
    if (entry === "start") await supervisor.start()
    else await supervisor.resume()
    await vi.waitFor(() => expect(publish).toHaveBeenCalledWith("state", expect.objectContaining({ status: "FAILED" })))
    expect(supervisor.state.status).toBe("FAILED")
    expect(loadResearchState(join(dir, ".athena", "state.json")).status).toBe("FAILED")
    expect(publish.mock.calls.filter(([kind]) => kind === "output")).toHaveLength(1)
    expect(publish).toHaveBeenCalledWith("output", {
      source: "supervisor", channel: "error", text: "research failed: ideation failed",
    })
  })

  it.each(["output", "state"])("preserves FAILED when its %s notification rejects", async (rejectedKind) => {
    const dir = tmpDir()
    const publish = vi.fn(async (kind: string, data: Record<string, unknown>) => {
      if (kind === rejectedKind && (kind === "output" || data.status === "FAILED")) throw new Error("offline")
    })
    const { supervisor } = coreSupervisor(dir, { workers: {
      publish,
      runIdeatorTurn: async () => { throw new Error("ideation failed") },
    } })
    await supervisor.start()
    expect(loadResearchState(join(dir, ".athena", "state.json")).status).toBe("FAILED")
    expect(publish).toHaveBeenCalledWith("state", expect.objectContaining({ status: "FAILED" }))
    expect(publish).toHaveBeenCalledWith("output", expect.objectContaining({ text: "research failed: ideation failed" }))
  })

  it("joins in-flight ideation before reporting STOPPED", async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    const runIdeatorTurn = vi.fn(async () => {
      await gate
      return [HypothesisSchema.parse({ statement: "idea", intervention: "change", expected_effect: "improve" })]
    })
    const runPlanAgentTurn = vi.fn(async () => null)
    const { supervisor, tree } = coreSupervisor(tmpDir(), { workers: { runIdeatorTurn, runPlanAgentTurn } })
    const loop = runSearch(supervisor)
    let stopped = false
    try {
      await vi.waitFor(() => expect(runIdeatorTurn).toHaveBeenCalledTimes(1))
      const stopping = supervisor.requestStop().then(() => { stopped = true })
      await new Promise<void>((resolve) => setImmediate(resolve))
      expect(stopped).toBe(false)
      release()
      await stopping
      await loop
      expect(tree.pendingHypotheses().filter((hypothesis) => hypothesis.statement === "idea")).toHaveLength(1)
      expect(runPlanAgentTurn).not.toHaveBeenCalled()
      expect((await supervisor.readState()).status).toBe("STOPPED")
    } finally {
      release()
      await supervisor.requestStop()
      await loop
    }
  })

  it("drains every running turn through settlement before STOPPED", async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    const runPlanAgentTurn = vi.fn(async () => {
      await gate
      return PlanDecisionSchema.parse({ decision: "abandon", reason: "done" })
    })
    const { supervisor, tree } = coreSupervisor(tmpDir(), { workers: { runPlanAgentTurn } })
    for (let i = 0; i < 2; i++) tree.addHypothesis(HypothesisSchema.parse({
      statement: `idea ${i}`, intervention: "change", expected_effect: "improve", parent_id: "exp_baseline",
    }))
    const loop = runSearch(supervisor)
    try {
      await vi.waitFor(() => expect(runPlanAgentTurn).toHaveBeenCalledTimes(2))
      const stopping = supervisor.requestStop()
      release()
      await stopping
      await loop
      expect(tree.experiments("search").map((experiment) => experiment.status)).toEqual(["FAILED", "FAILED"])
      expect(supervisor.state.plans).toEqual({})
      expect(supervisor.state.status).toBe("STOPPED")
    } finally {
      release()
      await supervisor.requestStop()
      await loop
    }
  })

  it("retains a Plan created during stop without launching its first turn", async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    const publish = vi.fn(async (_kind: string, data: Record<string, unknown>) => {
      if (data.plans && Object.keys(data.plans).length > 0) await gate
    })
    const runPlanAgentTurn = vi.fn(async () => null)
    const { supervisor, tree } = coreSupervisor(tmpDir(), { workers: { publish, runPlanAgentTurn } })
    const id = tree.addHypothesis(HypothesisSchema.parse({
      statement: "idea", intervention: "change", expected_effect: "improve", parent_id: "exp_baseline",
    }))
    const loop = runSearch(supervisor)
    try {
      await vi.waitFor(() => expect(publish).toHaveBeenCalledWith("state", expect.objectContaining({
        plans: expect.objectContaining({ [id]: expect.anything() }),
      })))
      const stopping = supervisor.requestStop()
      release()
      await stopping
      await loop
      expect(runPlanAgentTurn).not.toHaveBeenCalled()
      expect(supervisor.state.plans[id]!.turns_used).toBe(0)
      expect(supervisor.state.status).toBe("STOPPED")
    } finally {
      release()
      await supervisor.requestStop()
      await loop
    }
  })

  it("owns the loop before invoking a worker that rejoins SEARCH", async () => {
    let joined: Promise<void> | undefined
    const runIdeatorTurn = vi.fn(async () => {
      if (runIdeatorTurn.mock.calls.length === 1) joined = runSearch(supervisor)
      return []
    })
    const { supervisor } = coreSupervisor(tmpDir(), { workers: { runIdeatorTurn } })
    await runSearch(supervisor)
    await joined
    expect(runIdeatorTurn).toHaveBeenCalledTimes(1)
  })

  it("does not lose a resume while the WAITING publication is in flight", async () => {
    let release!: () => void
    const publication = new Promise<void>((resolve) => { release = resolve })
    const publish = vi.fn(async (_kind: string, data: Record<string, unknown>) => {
      if (data.status === "WAITING") await publication
    })
    const runPlanAgentTurn = vi.fn(async () => PlanDecisionSchema.parse({ decision: "abandon", reason: "done" }))
    const { supervisor, state, tree } = coreSupervisor(tmpDir(), { workers: { publish, runPlanAgentTurn } })
    state.manual_mode = true
    tree.addHypothesis(HypothesisSchema.parse({
      statement: "improve", intervention: "add feature", expected_effect: "raise metric",
      parent_id: "exp_baseline",
    }))
    const loop = runSearch(supervisor)
    try {
      await vi.waitFor(() => expect(publish).toHaveBeenCalledWith("state", expect.objectContaining({ status: "WAITING" })))
      await supervisor.setManualMode(false)
      release()
      await vi.waitFor(() => expect(runPlanAgentTurn).toHaveBeenCalledTimes(1), { timeout: 250 })
      await loop
    } finally {
      release()
      await supervisor.requestStop()
      await loop
    }
  })

  it("stops a waiting loop without dispatching work", async () => {
    const runIdeatorTurn = vi.fn(async () => [])
    const { supervisor } = coreSupervisor(tmpDir(), { workers: { runIdeatorTurn } })
    await supervisor.pause()
    const loop = runSearch(supervisor)
    await Promise.resolve()
    expect(await supervisor.requestStop()).toBe("STOPPED")
    await loop
    expect(runIdeatorTurn).not.toHaveBeenCalled()
    expect(supervisor.runningPlanIds).toEqual([])
  })

  it.each(["budget", "plan-budget", "mode", "resume", "phase"] as const)("wakes the existing SEARCH loop through %s", async (action) => {
    const runIdeatorTurn = vi.fn(async () => [])
    const { supervisor, state, tree } = coreSupervisor(tmpDir(), { workers: {
      runIdeatorTurn,
      runPlanAgentTurn: async () => PlanDecisionSchema.parse({ decision: "abandon", reason: "done" }),
    } })
    let planId = ""
    state.concurrency = 1
    if (action === "plan-budget") {
      planId = tree.addHypothesis(HypothesisSchema.parse({
        statement: "improve", intervention: "add feature", expected_effect: "raise metric",
        parent_id: "exp_baseline", turn_limit: 1,
      }))
      await supervisor["startPlan"](planId)
      state.plans[planId]!.turns_used = 1
    }
    await supervisor.pause()
    const loop = runSearch(supervisor)
    const joined = runSearch(supervisor)
    await Promise.resolve()
    try {
      expect(runIdeatorTurn).not.toHaveBeenCalled()
      if (action === "budget") await supervisor.configureSearch({ search_limit: 11 })
      else if (action === "plan-budget") await supervisor.updateWaitingPlanBudget({ plan_id: planId, turn_limit: 2 })
      else if (action === "mode") await supervisor.setManualMode(false)
      else if (action === "resume") await supervisor.resume()
      else await supervisor.setPhaseDecision("SEARCH")
      await vi.waitFor(() => expect(runIdeatorTurn).toHaveBeenCalledTimes(1), { timeout: 250 })
      await Promise.all([loop, joined])
    } finally {
      await supervisor.requestStop()
      await Promise.all([loop, joined])
    }
  })

  it.each([
    [false, false], [true, false], [false, true], [true, true],
  ])("checks real recovery prerequisites (missing context=%s, workspace=%s)", async (missingContext, missingWorkspace) => {
    const dir = tmpDir()
    const { supervisor, state, tree } = coreSupervisor(dir)
    const id = tree.addHypothesis(HypothesisSchema.parse({
      statement: "improve", intervention: "add feature", expected_effect: "raise metric",
      parent_id: "exp_baseline",
    }))
    await supervisor["startPlan"](id)
    if (missingContext) state.plans[id]!.context_ref = REF
    if (missingWorkspace) tree.getExperiment(`exp_${id}`).gitwork.path = join(dir, "absent")
    const plan = { ...state.plans[id] }

    const recovered = await supervisor.recover()

    expect(recovered.status).toBe(missingContext || missingWorkspace ? "WAITING" : "RUNNING")
    expect(recovered.plans[id]).toEqual(plan)
    expect(researchStateToJSON(loadResearchState(join(dir, ".athena", "state.json"))))
      .toEqual(researchStateToJSON(recovered))
  })

  it("reads plan workspaces from the durable tree after reconstruction", async () => {
    const dir = tmpDir()
    const { supervisor } = coreSupervisor(dir)
    const id = supervisor.tree.addHypothesis(HypothesisSchema.parse({
      statement: "improve", intervention: "add feature", expected_effect: "raise metric",
      parent_id: "exp_baseline",
    }))
    await supervisor["startPlan"](id)
    const expected = { path: join(dir, "worktrees", id), branch: id, base_commit: "c0" }
    expect(supervisor.workspace(id)).toEqual(expected)

    const restored = coreSupervisor(dir).supervisor
    restored.state = loadResearchState(join(dir, ".athena", "state.json"))
    restored.tree = ResearchTree.load(join(dir, ".athena", "research_tree.json"))
    await restored.recover()
    await restored["startPlan"](id)
    expect(restored.workspace(id)).toEqual(expected)
    expect(restored.workspace(id)).toBe(restored.tree.getExperiment(`exp_${id}`).gitwork)
  })

  it.each([
    ["submit", 1, 10, 0, REF, "settle"],
    ["abandon", 1, 10, 0, null, "settle"],
    ["continue", 1, 10, 3, REF, "settle"],
    ["continue", 10, 10, 0, REF, "settle"],
    ["continue", 10, 10, 0, null, "wait"],
    ["continue", 1, 10, 0, REF, "continue"],
    ["continue", 99, null, 0, REF, "continue"],
  ] as const)("uses the active settlement policy (%s, turns=%s, limit=%s, stale=%s)",
    (decision, turns, limit, stale, best, action) => {
      const { supervisor } = coreSupervisor(tmpDir())
      const state = PlanStateSchema.parse({
        kind: "SEARCH", context_ref: REF, turns_used: turns, turn_limit: limit,
        patience: 3, stale_rounds: stale, best_ref: best,
      })
      const result = supervisor["decideSettlement"](state, PlanDecisionSchema.parse({ decision, reason: "test" }))
      expect(result).toEqual({ action, best_ref: action === "settle" ? best : null })
    })

  it("persists structured task understanding through the single writer", async () => {
    const dir = tmpDir()
    const { supervisor, state } = coreSupervisor(dir)
    const understanding = { title: "titanic", task_type: "classification", primary_metric: "accuracy" }

    const result = await supervisor.recordTaskUnderstanding(understanding)

    expect(result).toEqual({ recorded: true, task_understanding: understanding })
    expect(state.task_understanding).toEqual(understanding)
    const persisted = loadResearchState(join(dir, ".athena", "state.json"))
    expect(persisted.task_understanding).toEqual(understanding)
  })

  it("freezes guidance into later Plan inputs", async () => {
    const dir = tmpDir()
    const { supervisor, state, store } = coreSupervisor(dir)
    const hypothesisId = supervisor.tree.addHypothesis(
      HypothesisSchema.parse({
        statement: "improve feature",
        intervention: "add feature",
        expected_effect: "raise metric",
        parent_id: "exp_baseline",
      })
    )

    await supervisor.recordGuidance("next hint", "next")
    await supervisor.recordGuidance("always apply this", "persistent")
    await supervisor["startPlan"](hypothesisId)

    const planInput = await supervisor.planInput(hypothesisId)
    expect(planInput.human_context).toBe("always apply this\nnext hint")
    expect(Object.keys(planInput)).toHaveLength(9)
    const legacyRef = await store.putText(JSON.stringify({
      ...planInput, active_ancestor_hypotheses: [],
      initial_turn_limit: 12, initial_patience: 4,
    }))
    state.plans[hypothesisId]!.context_ref = legacyRef
    expect(await supervisor.planInput(hypothesisId)).toEqual(planInput)
    expect(state.plans[hypothesisId]!.context_ref).toBe(legacyRef)
    expect(await store.getText(legacyRef)).toContain("initial_turn_limit")
  })

  it("configures search budget and snapshots hypotheses", async () => {
    const dir = tmpDir()
    const { supervisor, state } = coreSupervisor(dir)

    const result = await supervisor.configureSearch({ search_limit: 7, concurrency: 3 })
    expect(result).toEqual({ search_limit: 7, concurrency: 3 })
    expect(state.search_limit).toBe(7)
    expect(state.concurrency).toBe(3)

    const snapshot = await supervisor.readHypotheses()
    expect(snapshot).toMatchObject({ sota: "exp_baseline", attempts: 0, search_limit: 7 })
  })

  it("runs VALIDATE interactively and persists the final report ref", async () => {
    const dir = tmpDir()
    const { supervisor, state, store } = coreSupervisor(dir)

    await supervisor.setPhaseDecision("VALIDATE")

    expect(state.phase).toBe("COMPLETED")
    expect(state.status).toBe("COMPLETED")
    expect(state.validation).not.toBeNull()
    expect(typeof state.validation?.["report_ref"]).toBe("string")
    expect(Object.keys(state.validation!).sort()).toEqual([
      "final_test_score", "generalization_gap", "generalization_warning",
      "report_ref", "result_id", "status", "test_score",
    ])
    expect(loadResearchState(join(dir, ".athena", "state.json")).validation).toEqual(state.validation)
    expect(await store.getText(state.validation!["report_ref"] as string)).toContain("## 验证结果")
  })
})
