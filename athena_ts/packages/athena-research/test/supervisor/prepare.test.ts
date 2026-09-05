import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { LocalArtifactStore } from "@athena/core"
import { DataScriptBundleSchema } from "../../src/contracts.js"
import { freezeEvaluator } from "../../src/supervisor/prepare.js"

let tmp: string
afterEach(() => {
  if (tmp) rmSync(tmp, { recursive: true, force: true })
})

function tmpDir(): string {
  tmp = mkdtempSync(join(tmpdir(), "athena-prep-"))
  return tmp
}

const fakeScripts = {
  freeze: async (root: string, entrypoint: string) =>
    DataScriptBundleSchema.parse({ bundle_id: "b1", entrypoint }),
}

describe("freezeEvaluator", () => {
  it.each([
    ["not json", "must declare eval_script"],
    ['{"eval_script":42}', "must declare eval_script"],
    ['{"eval_script":"missing.py"}', "eval_script is missing"],
    ['{"eval_script":"evaluator"}', "must contain an entrypoint"],
  ])("rejects invalid evaluator declarations (%s)", async (spec, error) => {
    const dir = tmpDir()
    mkdirSync(join(dir, "evaluator"))
    writeFileSync(join(dir, "metric.json"), spec)
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    await expect(freezeEvaluator({ root: dir, scripts: fakeScripts as never, store })).rejects.toThrow(error)
  })

  it("freezes a file evaluator with nested labels", async () => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    writeFileSync(join(dir, "metric.json"), '{"eval_script":"score.py"}')
    writeFileSync(join(dir, "score.py"), "# evaluator")
    mkdirSync(join(dir, "labels", "nested"), { recursive: true })
    writeFileSync(join(dir, "labels", "nested", "values.csv"), "id,label\n1,1\n")
    const ref = await freezeEvaluator({ root: dir, scripts: fakeScripts as never, store })
    expect(JSON.parse(await store.getText(ref))).toMatchObject({ entrypoint: "score.py" })
  })

  it("freezes a directory evaluator and returns a bundle ref", async () => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    writeFileSync(join(dir, "metric.json"), JSON.stringify({ eval_script: "evaluator" }), "utf-8")
    mkdirSync(join(dir, "evaluator"), { recursive: true })
    writeFileSync(join(dir, "evaluator", "evaluate.py"), "# eval\n", "utf-8")
    writeFileSync(join(dir, "evaluator", "labels.csv"), "id,label\n", "utf-8")

    const ref = await freezeEvaluator({ root: dir, scripts: fakeScripts as never, store })

    const bundle = DataScriptBundleSchema.parse(JSON.parse(await store.getText(ref)))
    expect(bundle.entrypoint).toBe("evaluate.py")
    expect(bundle.bundle_id).toBe("b1")
  })

  it("rejects a missing metric.json", async () => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    await expect(
      freezeEvaluator({ root: dir, scripts: fakeScripts as never, store })
    ).rejects.toThrow(/metric\.json is missing/)
  })

  it("rejects missing labels", async () => {
    const dir = tmpDir()
    const store = new LocalArtifactStore(join(dir, "artifacts"))
    writeFileSync(join(dir, "metric.json"), JSON.stringify({ eval_script: "evaluator" }), "utf-8")
    mkdirSync(join(dir, "evaluator"), { recursive: true })
    writeFileSync(join(dir, "evaluator", "evaluate.py"), "# eval\n", "utf-8")

    await expect(
      freezeEvaluator({ root: dir, scripts: fakeScripts as never, store })
    ).rejects.toThrow(/eval labels are missing/)
  })
})
