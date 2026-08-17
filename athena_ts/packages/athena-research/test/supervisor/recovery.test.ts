import { describe, expect, it } from "vitest"
import {
  ExperimentPlanSchema,
  ExperimentSchema,
  GitWorkBranchSchema,
  HypothesisSchema,
  ResearchTree,
  parseOrThrow,
} from "@athena/core"
import { PlanStateSchema } from "../../src/supervisor/plans.js"
import { Recovery } from "../../src/supervisor/recovery.js"
import { ResearchState } from "../../src/supervisor/state.js"

const REF = "sha256:" + "a".repeat(64)

function state(): ResearchState {
  return new ResearchState({
    status: "RUNNING",
    phase: "SEARCH",
    search_limit: 10,
    concurrency: 4,
    plans: {
      h1: parseOrThrow(PlanStateSchema, {
        kind: "SEARCH",
        context_ref: REF,
        turns_used: 1,
        turn_limit: 12,
        patience: 4,
      }),
    },
  })
}

function tree(opts: { settled?: boolean } = {}): ResearchTree {
  const t = new ResearchTree()
  t.addHypothesis(
    HypothesisSchema.parse({
      id: "h1",
      statement: "claim",
      intervention: "change",
      expected_effect: "improve",
    })
  )
  if (opts.settled) {
    t.addExperiment(
      "e1",
      ExperimentSchema.parse({
        hypothesis_id: "h1",
        commit: "c1",
        plan: ExperimentPlanSchema.parse({
          kind: "search",
          change: "change",
          run_config_ref: REF,
          budget: {},
          acceptance_rule: "trusted score",
        }),
        gitwork: GitWorkBranchSchema.parse({
          path: "C:/worktrees/h1",
          branch: "athena/plan/h1",
          base_commit: "c0",
        }),
        status: "FAILED",
        error: "settled before crash",
      })
    )
  }
  return t
}

describe("Recovery", () => {
  it("tree settled but state active is reconciled once", () => {
    const reconciled = Recovery.reconcile(state(), tree({ settled: true }), {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect("h1" in reconciled.plans).toBe(false)
    expect(reconciled.status).toBe("RUNNING")
  })

  it("active plan without an experiment record is dropped as an orphan", () => {
    const reconciled = Recovery.reconcile(state(), tree(), {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect("h1" in reconciled.plans).toBe(false)
    expect(reconciled.status).toBe("RUNNING")
  })

  it("missing context keeps plan and marks research waiting", () => {
    const reconciled = Recovery.reconcile(state(), tree(), {
      workspaceExists: () => true,
      artifactExists: () => false,
    })
    expect("h1" in reconciled.plans).toBe(true)
    expect(reconciled.status).toBe("WAITING")
  })

  it("missing workspace keeps frozen plan and marks waiting", () => {
    const reconciled = Recovery.reconcile(state(), tree(), {
      workspaceExists: () => false,
      artifactExists: () => true,
    })
    expect(reconciled.plans["h1"]!.context_ref).toBe(REF)
    expect(reconciled.status).toBe("WAITING")
  })

  it("proposed hypothesis without plan stays queued; orphan plan is dropped", () => {
    const t = new ResearchTree()
    t.addHypothesis(
      HypothesisSchema.parse({
        id: "h_new",
        statement: "claim",
        intervention: "change",
        expected_effect: "improve",
      })
    )
    const reconciled = Recovery.reconcile(state(), t, {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect("h1" in reconciled.plans).toBe(false)
    expect(t.pendingHypotheses().length).toBeGreaterThan(0)
  })

  it("settled prepare plan is removed when baseline final experiment exists", () => {
    const s = new ResearchState({
      status: "RUNNING",
      phase: "SEARCH",
      search_limit: 10,
      concurrency: 4,
      plans: {
        prepare: parseOrThrow(PlanStateSchema, {
          kind: "PREPARE",
          context_ref: REF,
          turns_used: 0,
          turn_limit: 12,
        }),
      },
    })
    const t = new ResearchTree()
    t.addHypothesis(
      HypothesisSchema.parse({
        id: "h_baseline",
        statement: "claim",
        intervention: "change",
        expected_effect: "improve",
      })
    )
    t.addExperiment(
      "e_baseline",
      ExperimentSchema.parse({
        hypothesis_id: "h_baseline",
        commit: "c1",
        plan: ExperimentPlanSchema.parse({
          kind: "baseline",
          change: "baseline",
          run_config_ref: REF,
          budget: {},
          acceptance_rule: "trusted score",
        }),
        gitwork: GitWorkBranchSchema.parse({
          path: "C:/worktrees/prepare",
          branch: "athena/plan/prepare",
          base_commit: "c0",
        }),
        status: "SUCCEEDED",
        eval: { experiment_id: "e_baseline", primary: 0.5, per_sample: REF },
      })
    )
    t.updateHypothesisStatus("h_baseline", "SUPPORTED")

    const reconciled = Recovery.reconcile(s, t, {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect("prepare" in reconciled.plans).toBe(false)
  })

  it("non-terminal baseline keeps prepare plan", () => {
    const s = new ResearchState({
      status: "RUNNING",
      phase: "SEARCH",
      search_limit: 10,
      concurrency: 4,
      plans: {
        prepare: parseOrThrow(PlanStateSchema, {
          kind: "PREPARE",
          context_ref: REF,
          turns_used: 0,
          turn_limit: 12,
        }),
      },
    })
    const t = new ResearchTree()
    t.addHypothesis(
      HypothesisSchema.parse({
        id: "h_baseline",
        statement: "claim",
        intervention: "change",
        expected_effect: "improve",
      })
    )
    t.addExperiment(
      "e_baseline",
      ExperimentSchema.parse({
        hypothesis_id: "h_baseline",
        commit: "c1",
        plan: ExperimentPlanSchema.parse({
          kind: "baseline",
          change: "baseline",
          run_config_ref: REF,
          budget: {},
          acceptance_rule: "trusted score",
        }),
        gitwork: GitWorkBranchSchema.parse({
          path: "C:/worktrees/prepare",
          branch: "athena/plan/prepare",
          base_commit: "c0",
        }),
        status: "RUNNING",
      })
    )

    const reconciled = Recovery.reconcile(s, t, {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect("prepare" in reconciled.plans).toBe(true)
  })
})
