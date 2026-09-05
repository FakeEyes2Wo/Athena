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
import { reconcilePlans } from "../../src/supervisor/recovery.js"
import { parseResearchState, researchStateToJSON, type ResearchState } from "../../src/supervisor/state.js"

const REF = "sha256:" + "a".repeat(64)

function state(): ResearchState {
  return parseResearchState({
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

function tree(status?: "RUNNING" | "FAILED"): ResearchTree {
  const t = new ResearchTree()
  t.addHypothesis(
    HypothesisSchema.parse({
      id: "h1",
      statement: "claim",
      intervention: "change",
      expected_effect: "improve",
    })
  )
  if (status !== undefined) {
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
        status,
        error: status === "FAILED" ? "settled before crash" : null,
      })
    )
  }
  return t
}

describe("reconcilePlans", () => {
  it.each([null, { score: 0.5 }])("retains validation plans only without a result: %j", (validation) => {
    const original = parseResearchState({
      status: "RUNNING",
      phase: "VALIDATE",
      search_limit: 10,
      concurrency: 4,
      validation,
      plans: {
        validate: {
          kind: "VALIDATE",
          context_ref: REF,
          turns_used: 1,
          turn_limit: 12,
        },
      },
    })
    const reconciled = reconcilePlans(original, new ResearchTree(), {
      workspaceExists: () => false,
      artifactExists: () => false,
    })
    expect(reconciled.plans).toEqual(validation === null ? original.plans : {})
    expect(reconciled.status).toBe(validation === null ? "WAITING" : "RUNNING")
    expect(reconciled.validation).toEqual(validation)
  })

  it("retains a running search plan with available prerequisites unchanged", () => {
    const original = state()
    const reconciled = reconcilePlans(original, tree("RUNNING"), {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect(researchStateToJSON(reconciled)).toEqual(researchStateToJSON(original))
    expect(reconciled).not.toBe(original)
  })

  it("preserves all durable configuration and does not mutate the input", () => {
    const original = state()
    original.ideator_count = 5
    original.hypotheses_per_ideator = 4
    original.task_understanding = { title: "frozen research task" }
    const reconciled = reconcilePlans(original, tree("FAILED"), {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect(researchStateToJSON(reconciled)).toEqual({ ...researchStateToJSON(original), plans: {} })
    expect(original.plans).toHaveProperty("h1")
  })

  it("removes settled plans even when their old prerequisites are missing", () => {
    const reconciled = reconcilePlans(state(), tree("FAILED"), {
      workspaceExists: () => false,
      artifactExists: () => false,
    })
    expect(reconciled.plans).toEqual({})
    expect(reconciled.status).toBe("RUNNING")
  })

  it("tree settled but state active is reconciled once", () => {
    const reconciled = reconcilePlans(state(), tree("FAILED"), {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect("h1" in reconciled.plans).toBe(false)
    expect(reconciled.status).toBe("RUNNING")
  })

  it("active plan without an experiment record is dropped as an orphan", () => {
    const reconciled = reconcilePlans(state(), tree(), {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect("h1" in reconciled.plans).toBe(false)
    expect(reconciled.status).toBe("RUNNING")
  })

  it("missing context keeps plan and marks research waiting", () => {
    const reconciled = reconcilePlans(state(), tree("RUNNING"), {
      workspaceExists: () => true,
      artifactExists: () => false,
    })
    expect("h1" in reconciled.plans).toBe(true)
    expect(reconciled.status).toBe("WAITING")
  })

  it("missing workspace keeps frozen plan and marks waiting", () => {
    const reconciled = reconcilePlans(state(), tree("RUNNING"), {
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
    const reconciled = reconcilePlans(state(), t, {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect("h1" in reconciled.plans).toBe(false)
    expect(t.pendingHypotheses().length).toBeGreaterThan(0)
  })

  it("settled prepare plan is removed when baseline final experiment exists", () => {
    const s = parseResearchState({
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

    const reconciled = reconcilePlans(s, t, {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect("prepare" in reconciled.plans).toBe(false)
  })

  it("non-terminal baseline keeps prepare plan", () => {
    const s = parseResearchState({
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

    const reconciled = reconcilePlans(s, t, {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    expect("prepare" in reconciled.plans).toBe(true)
  })
})
