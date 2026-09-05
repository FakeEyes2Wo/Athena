import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import { LocalArtifactStore } from "@athena/core"
import { ContextManager } from "@athena/agent"
import { WorkerRunner, PlanDecisionOutputType } from "../src/worker.js"

let tmp: string
afterEach(() => {
  vi.unstubAllEnvs()
  if (tmp) rmSync(tmp, { recursive: true, force: true })
})

class ScriptedClient {
  requests: Record<string, unknown>[] = []
  constructor(private response: string) {}
  chat = {
    completions: {
      create: async (request: Record<string, unknown>) => {
        this.requests.push(request)
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
  it.each(["openai", "deepseek"])("preserves contexts and construction-time provider (%s)", async (kind) => {
    vi.stubEnv("LLM_PROVIDER", kind)
    tmp = mkdtempSync(join(tmpdir(), "athena-worker-"))
    const store = new LocalArtifactStore(join(tmp, "artifacts"))
    const client = new ScriptedClient('{"decision":"submit","reason":"done"}')
    const runner = new WorkerRunner({ store, model: "test-model", client })
    vi.stubEnv("LLM_PROVIDER", kind === "openai" ? "deepseek" : "openai")
    const schema = kind === "deepseek" ? [{
      role: "system", content: 'Return a JSON object matching this schema:\n{"type":"object"}',
    }] : []
    expect(runner).not.toHaveProperty("runText")
    await runner.runStructured("artifact://literal", PlanDecisionOutputType)
    await runner.runStructured("fresh", PlanDecisionOutputType)
    expect(client.requests[0]!["messages"]).toEqual([{ role: "user", content: "artifact://literal" }, ...schema])
    expect(client.requests[1]!["messages"]).toEqual([{ role: "user", content: "fresh" }, ...schema])
    const memory = new ContextManager()
    const opts = { memory, systemPrompt: "Be concise." }
    await runner.runStructured("first", PlanDecisionOutputType, opts)
    await runner.runStructured("second", PlanDecisionOutputType, opts)
    expect(client.requests[3]!["messages"]).toEqual([
      { role: "system", content: "Be concise." },
      { role: "user", content: "first" },
      { role: "assistant", content: '{"decision":"submit","reason":"done"}' },
      { role: "user", content: "second" },
      ...schema,
    ])
  })

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
