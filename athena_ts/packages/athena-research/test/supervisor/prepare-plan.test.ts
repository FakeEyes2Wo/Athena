import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { LocalArtifactStore, type GitWorkBranch } from "@athena/core"
import { DataScriptBundleSchema } from "../../src/contracts.js"
import { PlanDecisionSchema, PlanStateSchema } from "../../src/supervisor/plans.js"
import { PrepareResultSchema, runEvaluatorPlan, runPreparePlan } from "../../src/supervisor/prepare.js"
import { PlanTurnResultSchema } from "../../src/supervisor/experiment.js"

const REF = "sha256:" + "a".repeat(64)

let tmp: string
afterEach(() => {
  if (tmp) rmSync(tmp, { recursive: true, force: true })
})

function tmpDir(): string {
  tmp = mkdtempSync(join(tmpdir(), "athena-prep-"))
  return tmp
}

const submit = async () => PlanDecisionSchema.parse({ decision: "submit", reason: "done" })

const fakeScripts = {
  freeze: async (root: string, metadata: { entrypoint: string }) =>
    DataScriptBundleSchema.parse({ bundle_id: "b1", entrypoint: metadata.entrypoint }),
}

describe("runEvaluatorPlan", () => {
  it("freezes the evaluator on submit", async () => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    writeFileSync(join(dir, "metric.json"), JSON.stringify({ eval_script: "evaluate.py" }), "utf-8")
    writeFileSync(join(dir, "evaluate.py"), "# eval\n", "utf-8")
    writeFileSync(join(dir, "labels.csv"), "id,label\n", "utf-8")

    const ref = await runEvaluatorPlan({
      agent: { turn: submit },
      scripts: fakeScripts as never,
      store,
      evaluatorDir: dir,
      task: "build evaluator",
      maxTurns: 3,
    })

    const bundle = DataScriptBundleSchema.parse(JSON.parse(await store.getText(ref)))
    expect(bundle.bundle_id).toBe("b1")
  })

  it("abandon raises", async () => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    await expect(
      runEvaluatorPlan({
        agent: { turn: async () => PlanDecisionSchema.parse({ decision: "abandon", reason: "nope" }) },
        scripts: fakeScripts as never,
        store,
        evaluatorDir: dir,
        task: "build evaluator",
        maxTurns: 2,
      })
    ).rejects.toThrow(/abandoned/)
  })
})

describe("runPreparePlan", () => {
  it("returns PrepareResult on scored submit", async () => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    const branch: GitWorkBranch = { path: join(dir, "ws"), branch: "athena/prepare", base_commit: "c0" }
    mkdirSync(branch.path, { recursive: true })

    const result = await runPreparePlan({
      agent: { turn: submit },
      evaluator: { score: async () => ({ candidate_id: "prepare", test_score: 0.8, direction: "maximize" as const, kfold_mean: null, kfold_std: null }) },
      git: {} as never,
      workspace: branch,
      execution: { project_root: dir, environment_root: dir, ensureEnvironment: () => {}, run: async () => ({ ok: true, stdout: "", stderr: "", exit_code: 0 }) } as never,
      store,
      evaluatorRef: REF,
      treeRef: REF,
      task: "prepare baseline",
      maxTurns: 3,
      planRunnerFactory: () => ({
        runTurn: async () =>
          PlanTurnResultSchema.parse({
            kind: "scored",
            metric: 0.8,
            commit: "c1",
            next_state: PlanStateSchema.parse({ kind: "PREPARE", context_ref: REF, turns_used: 1, turn_limit: 3 }),
            predictions_ref: REF,
            evidence_ref: REF,
            report_ref: REF,
          }),
      }),
    })

    expect(PrepareResultSchema.parse(result).metric).toBe(0.8)
    expect(result.commit).toBe("c1")
  })
})
