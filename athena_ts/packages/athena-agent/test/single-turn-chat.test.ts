import { describe, expect, it } from "vitest"
import { CancellationToken } from "../src/cancel.js"
import { modelResponse, textPart } from "../src/messages.js"
import { ContextManager } from "../src/memory/context-manager.js"
import { singleTurnChat } from "../src/single-turn-chat.js"
import { tool, ToolRegistry } from "../src/tool.js"
import type { EmitEvent } from "../src/tool-types.js"

type ToolCallScript = { name: string; arguments: Record<string, unknown> }

class ScriptedClient {
  requests: Array<Record<string, unknown>> = []
  private responses: Array<string | null | ToolCallScript>
  chat = {
    completions: {
      create: async (kw: Record<string, unknown>) => {
        this.requests.push(kw)
        const response = this.responses.shift()
        let delta: { content?: string | null; tool_calls?: unknown }
        let finishReason: string
        if (response !== null && response !== undefined && typeof response === "object") {
          delta = {
            content: null,
            tool_calls: [
              {
                index: 0,
                id: "call-1",
                function: {
                  name: response.name,
                  arguments: JSON.stringify(response.arguments),
                },
              },
            ],
          }
          finishReason = "tool_calls"
        } else {
          delta = { content: response as string | null, tool_calls: null }
          finishReason = "stop"
        }
        const chunk = { choices: [{ delta, finish_reason: finishReason }] }
        return (async function* () {
          yield chunk
        })()
      },
    },
  }
  constructor(responses: Array<string | null | ToolCallScript>) {
    this.responses = [...responses]
  }
}

function toolCall(name: string, arguments_: Record<string, unknown>): ToolCallScript {
  return { name, arguments: arguments_ }
}

describe("single_turn_chat", () => {
  it("returns final text without reusing implicit history", async () => {
    const client = new ScriptedClient(["first answer", "second answer"])
    expect(await singleTurnChat("first", { model: "model", client })).toBe("first answer")
    expect(await singleTurnChat("second", { model: "model", client })).toBe("second answer")
    expect(client.requests[1]!["messages"]).toEqual([{ role: "user", content: "second" }])
  })

  it("treats artifact like prompt as literal text", async () => {
    const client = new ScriptedClient(["answer"])
    const result = await singleTurnChat("artifact://README.md", { model: "model", client })
    expect(result).toBe("answer")
    expect(client.requests[0]!["messages"]).toEqual([
      { role: "user", content: "artifact://README.md" },
    ])
  })

  it("reuses caller owned memory", async () => {
    const client = new ScriptedClient(["first answer", "second answer"])
    const memory = new ContextManager()
    await singleTurnChat("first", { model: "model", client, memory })
    const result = await singleTurnChat("second", { model: "model", client, memory })
    expect(result).toBe("second answer")
    expect(client.requests[1]!["messages"]).toEqual([
      { role: "user", content: "first" },
      { role: "assistant", content: "first answer" },
      { role: "user", content: "second" },
    ])
  })

  it.each([null, ""])("omits empty system prompt (%j)", async (systemPrompt) => {
    const client = new ScriptedClient(["answer"])
    await singleTurnChat("question", { model: "model", client, systemPrompt })
    expect(client.requests[0]!["messages"]).toEqual([{ role: "user", content: "question" }])
  })

  it("adds nonempty system prompt once to reused memory", async () => {
    const client = new ScriptedClient(["first answer", "second answer"])
    const memory = new ContextManager()
    await singleTurnChat("first", { model: "model", client, memory, systemPrompt: "Be concise." })
    await singleTurnChat("second", { model: "model", client, memory, systemPrompt: "Be concise." })
    expect(client.requests[1]!["messages"]).toEqual([
      { role: "system", content: "Be concise." },
      { role: "user", content: "first" },
      { role: "assistant", content: "first answer" },
      { role: "user", content: "second" },
    ])
  })

  it("executes tools and forwards events", async () => {
    const tools = new ToolRegistry()
    const calls: string[] = []
    const echo = tool(
      async (input: Record<string, unknown>) => {
        calls.push(input["text"] as string)
        return input["text"]
      },
      {
        name: "echo",
        inputSchema: { type: "object", properties: { text: { type: "string" } } },
      }
    )
    tools.register(echo)
    const client = new ScriptedClient([toolCall("echo", { text: "value" }), "final answer"])
    const events: Array<[string, string, Record<string, unknown> | null]> = []
    const emit: EmitEvent = async (kind, ref, data) => {
      events.push([kind, ref, data ?? null])
    }
    const result = await singleTurnChat("question", { model: "model", client, tools, emit })
    expect(result).toBe("final answer")
    expect(calls).toEqual(["value"])
    expect(events.map((e) => e[0])).toEqual([
      "agent/function_call",
      "tool/begin",
      "tool/end",
      "agent/text_delta",
    ])
  })

  const invalidCases: Array<[Record<string, unknown>, string]> = [
    [{ prompt: "", model: "model" }, "prompt"],
    [{ prompt: "question", model: "" }, "model"],
    [{ prompt: "question", model: "model", maxTurns: 0 }, "max_turns"],
    [{ prompt: "question", model: "model", maxTurns: true }, "max_turns"],
    [{ prompt: "question", model: "model", maxTokens: 0 }, "max_tokens"],
    [{ prompt: "question", model: "model", maxTokens: true }, "max_tokens"],
    [{ prompt: "question", model: "model", temperature: 2.1 }, "temperature"],
    [{ prompt: "question", model: "model", temperature: true }, "temperature"],
  ]

  it.each(invalidCases)("rejects invalid input before model call %j", async (opts, message) => {
    const client = new ScriptedClient(["unused"])
    const options: Record<string, unknown> = {
      model: opts["model"],
      maxTurns: opts["maxTurns"],
      maxTokens: opts["maxTokens"],
      temperature: opts["temperature"],
      client,
    }
    await expect(singleTurnChat(opts["prompt"] as string, options as never)).rejects.toThrow(
      message
    )
    expect(client.requests).toEqual([])
  })

  it("rejects pre-cancelled call before model execution", async () => {
    const client = new ScriptedClient(["unused"])
    const cancel = new CancellationToken()
    cancel.set()
    await expect(
      singleTurnChat("question", { model: "model", client, cancel })
    ).rejects.toBeInstanceOf(Error)
    expect(client.requests).toEqual([])
  })

  it("does not return old answer when current call has no text", async () => {
    const memory = new ContextManager()
    memory.append(modelResponse([textPart("old answer")]))
    const client = new ScriptedClient([null])
    await expect(
      singleTurnChat("question", { model: "model", client, memory })
    ).rejects.toThrow(/final assistant text/)
  })
})
