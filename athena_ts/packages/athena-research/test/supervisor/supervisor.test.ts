import { mkdirSync, mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
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
import { PlanBestSchema, PlanDecisionSchema, PlanStateSchema } from "../../src/supervisor/plans.js"
import { PrepareResultSchema } from "../../src/supervisor/prepare.js"
import { ResearchState } from "../../src/supervisor/state.js"
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
    const state = new ResearchState({
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
  const state = new ResearchState({
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

describe("FixedFlowSupervisor core actions", () => {
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
    const persisted = ResearchState.load(join(dir, ".athena", "state.json"))
    expect(persisted.task_understanding).toEqual(understanding)
  })

  it("freezes guidance into later Plan inputs", async () => {
    const dir = tmpDir()
    const { supervisor } = coreSupervisor(dir)
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
    await supervisor.startPlan(hypothesisId)

    const planInput = await supervisor.planInput(hypothesisId)
    expect(planInput.human_context).toBe("always apply this\nnext hint")
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
    expect(ResearchState.load(join(dir, ".athena", "state.json")).validation).toEqual(state.validation)
    expect(await store.getText(state.validation!["report_ref"] as string)).toContain("## 验证结果")
  })
})
