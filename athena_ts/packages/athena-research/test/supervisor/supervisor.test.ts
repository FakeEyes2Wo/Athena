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
