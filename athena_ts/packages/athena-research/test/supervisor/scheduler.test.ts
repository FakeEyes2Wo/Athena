import { describe, expect, it } from "vitest"
import { ExperimentSchema, HypothesisSchema, ResearchTree, parseOrThrow } from "@athena/core"
import { PlanStateSchema } from "../../src/contracts.js"
import { EloPolicy } from "../../src/supervisor/ranker.js"
import { parseResearchState, researchStateToJSON, type ResearchState } from "../../src/supervisor/state.js"
import { Scheduler, countSearchAttempts } from "../../src/supervisor/scheduler.js"

const CONTEXT_REF = "sha256:" + "d".repeat(64)
const BEST_REF = "sha256:" + "b".repeat(64)

function plan(opts: { turns?: number; limit?: number | null; best?: string | null } = {}) {
  return parseOrThrow(PlanStateSchema, {
    kind: "SEARCH",
    context_ref: CONTEXT_REF,
    turns_used: opts.turns ?? 0,
    turn_limit: opts.limit === undefined ? 12 : opts.limit,
    patience: 4,
    best_ref: opts.best === undefined ? BEST_REF : opts.best,
  })
}

function state(
  plans: Record<string, ReturnType<typeof plan>> = {},
  opts: { concurrency?: number; searchLimit?: number; manual?: boolean } = {}
): ResearchState {
  return parseResearchState({
    status: "RUNNING",
    phase: "SEARCH",
    search_limit: opts.searchLimit ?? 10,
    concurrency: opts.concurrency ?? 4,
    manual_mode: opts.manual ?? false,
    plans,
  })
}

function tree(...hypothesisIds: string[]): ResearchTree {
  const t = new ResearchTree()
  hypothesisIds.forEach((id, order) => {
    t.addHypothesis(
      HypothesisSchema.parse({
        id,
        statement: `claim ${id}`,
        intervention: `change ${id}`,
        expected_effect: "improve trusted metric",
        priority: 1000.0,
        order,
      })
    )
  })
  return t
}

function finalExperiment(hypothesisId: string, kind = "search", experimentId: string) {
  return ExperimentSchema.parse({
    parent_id: null,
    hypothesis_id: hypothesisId,
    commit: "abc123",
    plan: {
      kind,
      change: `test ${hypothesisId}`,
      run_config_ref: "artifact://run-config",
      budget: {},
      acceptance_rule: "trusted evaluation completes",
    },
    gitwork: {
      path: `C:/worktrees/${hypothesisId}`,
      branch: `search/${hypothesisId}`,
      base_commit: "base123",
    },
    status: "SUCCEEDED",
    eval: {
      experiment_id: experimentId,
      primary: 0.8,
      per_sample: BEST_REF,
    },
  })
}

function runningExperiment(hypothesisId: string, experimentId: string, kind = "search") {
  return ExperimentSchema.parse({
    parent_id: null,
    hypothesis_id: hypothesisId,
    commit: "abc124",
    plan: {
      kind,
      change: `test ${hypothesisId}`,
      run_config_ref: "artifact://run-config",
      budget: {},
      acceptance_rule: "trusted evaluation completes",
    },
    gitwork: {
      path: `C:/worktrees/${hypothesisId}`,
      branch: `search/${hypothesisId}`,
      base_commit: "base123",
    },
    status: "RUNNING",
  })
}

function treeWithFinal(hypothesisId: string, kind = "search"): ResearchTree {
  const t = new ResearchTree()
  t.addHypothesis(
    HypothesisSchema.parse({
      id: hypothesisId,
      statement: "claim",
      intervention: "change",
      expected_effect: "improve",
    })
  )
  const experimentId = `exp_${hypothesisId}`
  t.addExperiment(experimentId, finalExperiment(hypothesisId, kind, experimentId))
  return t
}

describe("Scheduler", () => {
  it("uses one injected policy for ranking, seeding and settlement", () => {
    class ReversePriority extends EloPolicy {
      override priority(hypothesis: ReturnType<typeof HypothesisSchema.parse>): number {
        return -hypothesis.priority
      }
    }
    const policy = new ReversePriority(64)
    const scheduler = new Scheduler(policy)
    const t = tree("h_high", "h_low")
    t.getHypothesis("h_high").priority = 1200
    expect(scheduler.policy).toBe(policy)
    expect(scheduler.policy.seed(t.getHypothesis("h_high"))).toBe(1200)
    expect(scheduler.policy.settle(1200, "WIN")).toBe(1232)
    expect(scheduler.nextActions(state({}, { concurrency: 1 }), t)).toEqual([
      { kind: "START_NEW", planId: "h_low" },
    ])
  })

  it("resumes unlimited plans even when the creation budget is exhausted", () => {
    const s = state({ h_old: plan({ turns: 100, limit: null, best: null }) }, { searchLimit: 1 })
    expect(s.plans["h_old"]!.best_ref).toBeNull()
    expect(new Scheduler().nextActions(s, tree("h_old", "h_new"))).toEqual([
      { kind: "RESUME", planId: "h_old" },
    ])
  })

  it("does not schedule outside SEARCH or beyond occupied capacity", () => {
    const scheduler = new Scheduler()
    const s = state({}, { concurrency: 1 })
    expect(scheduler.nextActions(s, tree("h_new"), { running: ["h_running"] })).toEqual([])
    s.phase = "PREPARE"
    expect(scheduler.nextActions(s, tree("h_new"))).toEqual([])
  })

  it("deduplicates only this selection without mutating durable inputs", () => {
    const t = tree("h_first", "h_duplicate")
    t.getHypothesis("h_duplicate").statement = t.getHypothesis("h_first").statement
    t.getHypothesis("h_duplicate").intervention = t.getHypothesis("h_first").intervention
    const s = state({}, { concurrency: 2 })
    const beforeTree = JSON.stringify(t.toDict())
    const beforeState = researchStateToJSON(s)
    expect(new Scheduler().nextActions(s, t)).toEqual([
      { kind: "START_NEW", planId: "h_first" },
      { kind: "GENERATE", count: 1 },
    ])
    expect(JSON.stringify(t.toDict())).toBe(beforeTree)
    expect(researchStateToJSON(s)).toEqual(beforeState)
    expect(t.pendingHypotheses()).toHaveLength(2)
  })

  it("completed slot is refilled without batch barrier", () => {
    const s = state({ h1: plan(), h2: plan(), h3: plan() })
    const t = tree("h1", "h2", "h3", "h5")
    expect(new Scheduler().nextActions(s, t, { running: ["h1", "h2", "h3"] })).toEqual([
      { kind: "START_NEW", planId: "h5" },
    ])
  })

  it("waiting plan releases slot and resumes before new plan", () => {
    const s = state({ h_old: plan({ turns: 3, limit: 3, best: null }) }, { concurrency: 1 })
    const t = tree("h_old", "h_new")

    expect(new Scheduler().nextActions(s, t)).toEqual([{ kind: "START_NEW", planId: "h_new" }])

    s.plans["h_old"]!.turn_limit = 5
    expect(new Scheduler().nextActions(s, t)).toEqual([{ kind: "RESUME", planId: "h_old" }])
  })

  it("policy priority and fifo order new hypotheses", () => {
    const t = tree("h_fifo_first", "h_high", "h_fifo_second")
    t.getHypothesis("h_high").priority = 1016.0
    const s = state({}, { concurrency: 3 })

    expect(new Scheduler().nextActions(s, t)).toEqual([
      { kind: "START_NEW", planId: "h_high" },
      { kind: "START_NEW", planId: "h_fifo_first" },
      { kind: "START_NEW", planId: "h_fifo_second" },
    ])
  })

  it("human next precedes policy queue without priority mutation", () => {
    const t = tree("h_high", "h_requested")
    t.getHypothesis("h_high").priority = 2000.0
    const requestedPriority = t.getHypothesis("h_requested").priority

    const actions = new Scheduler().nextActions(
      state({}, { concurrency: 1 }),
      t,
      { humanNext: "h_requested" }
    )

    expect(actions).toEqual([{ kind: "START_NEXT_HYPOTHESIS", planId: "h_requested" }])
    expect(t.getHypothesis("h_requested").priority).toBe(requestedPriority)
  })

  it("generate fills only remaining slots and attempt budget", () => {
    const s = state({ h_active: plan() }, { concurrency: 4, searchLimit: 3 })
    const t = tree("h_active")

    expect(new Scheduler().nextActions(s, t, { running: ["h_active"] })).toEqual([
      { kind: "GENERATE", count: 2 },
    ])
  })

  it("manual mode does not auto start pending hypotheses", () => {
    const t = tree("h1", "h2")
    const s = state({}, { concurrency: 2, manual: true })

    expect(new Scheduler().nextActions(s, t)).toEqual([])
  })

  it("manual mode generates only when no pending hypotheses", () => {
    const s = state({}, { concurrency: 2, manual: true })
    expect(new Scheduler().nextActions(s, new ResearchTree())).toEqual([
      { kind: "GENERATE", count: 2 },
    ])
  })

  it("manual mode starts human selected hypothesis", () => {
    const t = tree("h1", "h2")
    const s = state({}, { concurrency: 2, manual: true })
    expect(
      new Scheduler().nextActions(s, t, { humanNext: "h2" })
    ).toEqual([{ kind: "START_NEXT_HYPOTHESIS", planId: "h2" }])
  })

  it("fresh search asks for all unfilled slots", () => {
    expect(new Scheduler().nextActions(state(), new ResearchTree())).toEqual([
      { kind: "GENERATE", count: 4 },
    ])
  })

  it("prepare plan does not count as search attempt", () => {
    const prepare = parseOrThrow(PlanStateSchema, {
      kind: "PREPARE",
      context_ref: CONTEXT_REF,
      turns_used: 0,
      turn_limit: 12,
    })
    const s = state({ prepare }, { concurrency: 2, searchLimit: 2 })

    expect(new Scheduler().nextActions(s, new ResearchTree())).toEqual([
      { kind: "GENERATE", count: 2 },
    ])
  })
})

describe("countSearchAttempts", () => {
  it("settled search experiment counts as attempt", () => {
    expect(countSearchAttempts(state(), treeWithFinal("h1"))).toBe(1)
  })

  it("final search experiment counts after hypothesis settlement", () => {
    const t = treeWithFinal("h1")
    t.updateHypothesisStatus("h1", "SUPPORTED")
    expect(countSearchAttempts(state(), t)).toBe(1)
  })

  it("active search plan counts as attempt", () => {
    expect(countSearchAttempts(state({ h1: plan() }), new ResearchTree())).toBe(1)
  })

  it("narrow experiments query filters by plan kind", () => {
    const t = tree("h_settled", "h_running")
    t.addExperiment("e_settled", finalExperiment("h_settled", "search", "e_settled"))
    t.addExperiment("e_running", runningExperiment("h_running", "e_running"))

    expect(t.experiments().map((e) => e.hypothesis_id)).toEqual(["h_settled", "h_running"])
    expect(t.experiments("search").map((e) => e.hypothesis_id)).toEqual([
      "h_settled",
      "h_running",
    ])
    expect(t.experiments("baseline")).toEqual([])
  })

  it("counts only terminal search experiments", () => {
    const t = tree("h_settled", "h_running")
    t.addExperiment("e_settled", finalExperiment("h_settled", "search", "e_settled"))
    t.addExperiment("e_running", runningExperiment("h_running", "e_running"))

    expect(countSearchAttempts(state(), t)).toBe(1)
  })
})
