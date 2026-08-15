import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { LocalArtifactStore } from "@athena/core"
import { WorkerRunner, PlanDecisionOutputType } from "../src/worker.js"

let tmp: string
afterEach(() => {
  if (tmp) rmSync(tmp, { recursive: true, force: true })
})

class ScriptedClient {
  constructor(private response: string) {}
  chat = {
    completions: {
      create: async () => {
        const response = this.response
        return (async function* () {
          yield {
            choices: [
              { delta: { content: response, tool_calls: null }, finish_reason: "stop" },
            ],
          }
        })()
      },
    },
  }
}

describe("WorkerRunner", () => {
  it("runs a structured turn and parses the result", async () => {
    tmp = mkdtempSync(join(tmpdir(), "athena-worker-"))
    const store = new LocalArtifactStore(join(tmp, "artifacts"))
    const runner = new WorkerRunner({
      store,
      model: "test-model",
      client: new ScriptedClient('{"decision":"submit","reason":"done"}'),
    })

    const decision = await runner.runStructured('{"decision":"continue","reason":"x"}', PlanDecisionOutputType)

    expect(decision).toEqual({ decision: "submit", reason: "done", suggestions: [] })
  })

  it("returns null when structured parse fails", async () => {
    tmp = mkdtempSync(join(tmpdir(), "athena-worker-"))
    const store = new LocalArtifactStore(join(tmp, "artifacts"))
    const runner = new WorkerRunner({
      store,
      model: "test-model",
      client: new ScriptedClient("not json"),
    })

    await expect(runner.planDecision("prompt")).resolves.toBeNull()
  })
})
