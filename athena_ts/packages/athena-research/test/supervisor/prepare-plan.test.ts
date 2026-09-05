import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { LocalArtifactStore, type GitWorkBranch } from "@athena/core"
import { DataScriptBundleSchema } from "../../src/contracts.js"
import { PlanDecisionSchema } from "../../src/supervisor/plans.js"
import { PrepareResultSchema, runEvaluatorPlan, runPreparePlan } from "../../src/supervisor/prepare.js"

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
  freeze: async (root: string, entrypoint: string) =>
    DataScriptBundleSchema.parse({ bundle_id: "b1", entrypoint }),
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
  it.each([false, true])("returns a real PlanRunner result with feedback retry (%s)", async (failFirst) => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    const branch: GitWorkBranch = { path: join(dir, "ws"), branch: "athena/prepare", base_commit: "c0" }
    mkdirSync(branch.path, { recursive: true })
    mkdirSync(join(branch.path, "predictions"))
    writeFileSync(join(branch.path, "predictions", "values.csv"), "id,pred\n1,0.8\n")
    writeFileSync(join(branch.path, "report.md"), "# baseline report")
    writeFileSync(join(branch.path, "experiment.json"), JSON.stringify({
      version: 1, commands: [["train"]], outputs: { predictions: "predictions", report: "report.md" },
    }))
    const evaluatorRef = await store.putText(JSON.stringify(DataScriptBundleSchema.parse({
      bundle_id: "eval", entrypoint: "evaluate.py",
    })))
    const prompts: string[] = []
    const commits: string[] = []
    let executions = 0

    const result = await runPreparePlan({
      agent: { turn: async (prompt) => { prompts.push(prompt); return submit() } },
      evaluator: { score: async () => ({ candidate_id: "prepare", test_score: 0.8, direction: "maximize" as const, kfold_mean: null, kfold_std: null }) },
      git: {
        diff: async (workspace: GitWorkBranch) => {
          expect(workspace).toBe(branch)
          return { ref: REF, paths: ["experiment.json"] }
        },
        commit: async (_branch: GitWorkBranch, _diff: unknown, message: string) => {
          commits.push(message)
          return "c1"
        },
      } as never,
      workspace: branch,
      execution: { run: async (opts) => {
        expect(opts.workdir).toBe(branch.path)
        expect(opts.argv).toEqual(["train"])
        executions++
        return failFirst && executions === 1
          ? { ok: false, stdout: "", stderr: "retry command", exit_code: 1 }
          : { ok: true, stdout: "", stderr: "", exit_code: 0 }
      } },
      store,
      evaluatorRef,
      treeRef: REF,
      task: "prepare baseline",
      maxTurns: 3,
    })

    expect(PrepareResultSchema.parse(result).metric).toBe(0.8)
    expect(result.commit).toBe("c1")
    expect(result.evaluator_ref).toBe(evaluatorRef)
    expect(await store.getText(result.report_ref)).toBe("# baseline report")
    expect(JSON.parse(await store.getText(result.evidence_ref))).toMatchObject({ plan: "prepare", metric: 0.8, commit: "c1" })
    expect(commits).toHaveLength(1)
    expect(executions).toBe(failFirst ? 2 : 1)
    expect(prompts[0]).toBe("prepare baseline")
    if (failFirst) expect(prompts[1]).toContain("retry command")
  })
})
