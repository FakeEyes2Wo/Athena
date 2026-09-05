import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { AthenaThreadSchema, AthenaTurnSchema, LocalArtifactStore, type AthenaThread, type AthenaTurn } from "@athena/core"
import { ContextManager } from "../../src/memory/context-manager.js"
import { BaseTool, ToolRegistry } from "../../src/tool.js"
import { ToolContext, ToolResult, ToolSpec, type AskUser, type EmitEvent } from "../../src/tool-types.js"
import {
  Agent,
  BaseAgent,
  agentRunner,
  createAgent,
  createCodeAgent,
  type StructuredOutputType,
} from "../../src/agent/runtime.js"
import { AgentConfig, AgentContext, AgentOutcome } from "../../src/agent/models.js"
import {
  AnthropicProvider,
  DeepSeekProvider,
  OpenAIProvider,
  ResponsesProvider,
  StreamEvent,
  createProvider,
} from "../../src/agent/provider.js"
import { RequestUserInputTool } from "../../src/agent/tools/user-input.js"

function thread(id = "t1"): AthenaThread {
  return AthenaThreadSchema.parse({
    thread_id: id,
    session_id: "s1",
    status: "running",
    context_ref: "ctx://0",
  })
}

function turn(threadId = "t1", id = "t1.1"): AthenaTurn {
  return AthenaTurnSchema.parse({
    turn_id: id,
    thread_id: threadId,
    request_ref: "req://1",
    status: "running",
  })
}

async function noopEmit(): Promise<void> {}

function ctx(
  tools: ToolRegistry,
  opts: {
    memory?: ContextManager | null
    emit?: EmitEvent
    askUser?: AskUser | null
  } = {}
): AgentContext {
  return new AgentContext(
    thread(),
    turn(),
    opts.emit ?? noopEmit,
    tools,
    new AbortController().signal,
    opts.memory ?? null,
    null,
    [],
    opts.askUser ?? null
  )
}

class EchoTool extends BaseTool {
  readonly spec = new ToolSpec("echo", "echo", {})
  execute(input: Record<string, unknown>): ToolResult {
    return new ToolResult(input)
  }
}

const structuredOut: StructuredOutputType = {
  name: "_Out",
  modelJsonSchema() {
    return { type: "object", properties: { answer: { type: "string" } } }
  },
  parseJson(text: string) {
    const v = JSON.parse(text)
    if (typeof (v as { answer?: unknown }).answer !== "string") throw new Error("invalid")
    return v
  },
  toJson(value: unknown) {
    return JSON.stringify(value)
  },
}

class CaptureClient {
  kwargs: Record<string, unknown> = {}
  chat = {
    completions: {
      create: async (kw: Record<string, unknown>) => {
        this.kwargs = kw
        return (async function* () {
          yield { choices: [] }
        })()
      },
    },
  }
}

class BadRequestError extends Error {
  status = 400
  status_code = 400
}

class ResponseFormatFallbackClient {
  calls: Array<Record<string, unknown>> = []
  constructor(private message: string) {}
  chat = {
    completions: {
      create: async (kw: Record<string, unknown>) => {
        this.calls.push(kw)
        if (this.calls.length === 1) throw new BadRequestError(this.message)
        return (async function* () {
          yield { choices: [] }
        })()
      },
    },
  }
}

describe("Agent", () => {
  it("model combines name and client", () => {
    const client = {}
    const model = new ResponsesProvider("test-model", { client: client as never })
    expect(model.modelName).toBe("test-model")
    expect(model.client).toBe(client)
  })

  it("create_agent uses injected model client", () => {
    const client = {}
    const tools = new ToolRegistry()
    const agent = createAgent("test-model", tools, "system", { client: client as never })
    expect(agent.model.modelName).toBe("test-model")
    expect(agent.model.client).toBe(client)
  })

  it("environment builds three independent core agents", () => {
    const model = new ResponsesProvider("test-model", { client: {} as never })
    const agents = ["code", "data", "plot"].map((name) =>
      createCodeAgent(model, new ToolRegistry(), `${name} prompt`, new AgentConfig(200, 4096, 0.1, `${name}-agent`))
    )
    expect(agents.map((a) => a.name)).toEqual(["code-agent", "data-agent", "plot-agent"])
    expect(new Set(agents).size).toBe(3)
  })

  it("run receives context", async () => {
    const calls: AgentContext[] = []
    class SpyAgent extends BaseAgent {
      name = "spy"
      description = "记录调用以供测试。"
      async run(c: AgentContext): Promise<AgentOutcome> {
        calls.push(c)
        return new AgentOutcome("result://ok", "context://next")
      }
    }
    const tools = new ToolRegistry()
    const runner = agentRunner(new SpyAgent(), tools)

    const outcome = await runner(thread(), turn(), noopEmit)
    expect(outcome.resultRef).toBe("result://ok")
    expect(outcome.nextContextRef).toBe("context://next")
    expect(calls.length).toBe(1)
    expect(calls[0]!.thread.thread_id).toBe("t1")
    expect(calls[0]!.turn.turn_id).toBe("t1.1")
    expect(calls[0]!.tools).toBe(tools)
    expect(calls[0]!.cancel).toBeInstanceOf(AbortSignal)
  })

  it("tool invoke", async () => {
    class ToolUsingAgent extends BaseAgent {
      name = "tool_user"
      description = "使用工具。"
      async run(c: AgentContext): Promise<AgentOutcome> {
        const r = await this.tool(c, "echo", { msg: "hello" })
        return new AgentOutcome((r.data as { msg: string }).msg, c.thread.context_ref)
      }
    }
    const tools = new ToolRegistry()
    tools.register(new EchoTool())
    const runner = agentRunner(new ToolUsingAgent(), tools)
    const outcome = await runner(thread(), turn(), noopEmit)
    expect(outcome.resultRef).toBe("hello")
  })

  it("empty system prompt is not added to memory", async () => {
    class TextProvider {
      async *stream(_config: unknown, _tools: unknown, messages: Array<{ parts: Array<{ part_kind: string }> }>) {
        for (const message of messages) {
          for (const part of message.parts) {
            expect(part.part_kind).not.toBe("system-prompt")
          }
        }
        yield new StreamEvent("text_delta", { delta: "answer", accumulated: "answer" })
        yield new StreamEvent("response_completed")
      }
    }
    const tools = new ToolRegistry()
    const agent = new Agent(new ResponsesProvider("model", { client: {} as never }), tools, "")
    agent.model = new TextProvider() as never
    const memory = new ContextManager()
    const c = ctx(tools, { memory })
    await agent.run(c)
    for (const message of memory.items) {
      for (const part of message.parts) {
        expect(part.part_kind).not.toBe("system-prompt")
      }
    }
  })

  it("provider error is not reported as success", async () => {
    class ErrorProvider {
      async *stream() {
        yield new StreamEvent("error", { message: "provider failed" })
      }
    }
    const tools = new ToolRegistry()
    const agent = new Agent(new ResponsesProvider("model", { client: {} as never }), tools, "system")
    agent.model = new ErrorProvider() as never
    await expect(agent.run(ctx(tools))).rejects.toThrow(/provider failed/)
  })

  it("function_call emitted before tool output and next text", async () => {
    class ProbeTool extends BaseTool {
      readonly spec = new ToolSpec("probe", "probe", {})
      execute(): string {
        return "tool result"
      }
    }
    class ToolThenTextProvider {
      calls = 0
      async *stream() {
        this.calls += 1
        if (this.calls === 1) {
          yield new StreamEvent("function_call", {
            call_id: "call-1",
            name: "probe",
            arguments: { path: "data.csv" },
          })
        } else {
          yield new StreamEvent("text_delta", { delta: "continue", accumulated: "continue" })
        }
        yield new StreamEvent("response_completed")
      }
    }
    const tools = new ToolRegistry()
    tools.register(new ProbeTool())
    const agent = new Agent(new ResponsesProvider("model", { client: {} as never }), tools, "system")
    agent.model = new ToolThenTextProvider() as never
    const emitted: Array<[string, Record<string, unknown> | null]> = []
    const emit: EmitEvent = async (kind, _ref, data) => {
      emitted.push([kind, data ?? null])
    }
    await agent.run(ctx(tools, { emit }))
    const visible = emitted.filter(([kind]) =>
      ["agent/function_call", "tool/begin", "tool/end", "agent/text_delta"].includes(kind)
    )
    expect(visible.map(([kind]) => kind)).toEqual([
      "agent/function_call",
      "tool/begin",
      "tool/end",
      "agent/text_delta",
    ])
    expect(visible[0]![1]).toEqual({ name: "probe", arguments: { path: "data.csv" } })
  })

  it("non-concurrency-safe tool is a barrier", async () => {
    const timeline: string[] = []
    class UnsafeTool extends BaseTool {
      readonly spec = new ToolSpec("unsafe", "unsafe", {}, false)
      async execute(): Promise<string> {
        timeline.push("unsafe:start")
        await new Promise((r) => setTimeout(r, 0))
        timeline.push("unsafe:end")
        return "unsafe"
      }
    }
    class SafeTool extends BaseTool {
      readonly spec = new ToolSpec("safe", "safe", {})
      execute(): string {
        timeline.push("safe:start")
        return "safe"
      }
    }
    class CallsProvider {
      calls = 0
      async *stream() {
        this.calls += 1
        if (this.calls === 1) {
          yield new StreamEvent("function_call", { call_id: "1", name: "unsafe", arguments: {} })
          yield new StreamEvent("function_call", { call_id: "2", name: "safe", arguments: {} })
        } else {
          yield new StreamEvent("text_delta", { delta: "done", accumulated: "done" })
        }
        yield new StreamEvent("response_completed")
      }
    }
    const tools = new ToolRegistry()
    tools.register(new UnsafeTool())
    tools.register(new SafeTool())
    const agent = new Agent(new ResponsesProvider("model", { client: {} as never }), tools, "system")
    agent.model = new CallsProvider() as never
    await agent.run(ctx(tools))
    expect(timeline).toEqual(["unsafe:start", "unsafe:end", "safe:start"])
  })
})

describe("provider stream", () => {
  it("sets response_format when output type given", async () => {
    const client = new CaptureClient()
    const provider = new OpenAIProvider("model", { client: client as never })
    const events = []
    for await (const e of provider.stream(new AgentConfig(), new ToolRegistry(), [], new AbortController().signal, { outputType: structuredOut })) {
      events.push(e)
    }
    const rf = client.kwargs["response_format"] as Record<string, unknown>
    expect(rf["type"]).toBe("json_schema")
    expect((rf["json_schema"] as Record<string, unknown>)["name"]).toBe("_Out")
    expect(events.some((e) => e.kind === "response_completed")).toBe(true)
  })

  it("omits response_format without output type", async () => {
    const client = new CaptureClient()
    const provider = new ResponsesProvider("model", { client: client as never })
    const events = []
    for await (const e of provider.stream(new AgentConfig(), new ToolRegistry(), [], new AbortController().signal)) {
      events.push(e)
    }
    expect("response_format" in client.kwargs).toBe(false)
    expect(events.some((e) => e.kind === "response_completed")).toBe(true)
  })

  it("retries without unsupported response format", async () => {
    const client = new ResponseFormatFallbackClient("This response_format type is unavailable now")
    const provider = new OpenAIProvider("model", { client: client as never })
    const events = []
    for await (const e of provider.stream(new AgentConfig(), new ToolRegistry(), [], new AbortController().signal, { outputType: structuredOut })) {
      events.push(e)
    }
    expect(client.calls.length).toBe(2)
    expect((client.calls[0]!["response_format"] as Record<string, unknown>)["type"]).toBe("json_schema")
    expect("response_format" in client.calls[1]!).toBe(false)
    expect(events.some((e) => e.kind === "response_completed")).toBe(true)
  })

  it("does not retry unrelated bad request", async () => {
    const client = new ResponseFormatFallbackClient("invalid model")
    const provider = new ResponsesProvider("model", { client: client as never })
    await expect(
      (async () => {
        for await (const _e of provider.stream(new AgentConfig(), new ToolRegistry(), [], new AbortController().signal, { outputType: structuredOut })) {
          // drain
        }
      })()
    ).rejects.toThrow(/invalid model/)
    expect(client.calls.length).toBe(1)
  })

  it("disables deepseek thinking", async () => {
    const client = new CaptureClient()
    const provider = new ResponsesProvider("model", { client: client as never })
    const it = provider.stream(new AgentConfig(), new ToolRegistry(), [], new AbortController().signal)
    await it.next()
    expect(client.kwargs["extra_body"]).toEqual({ thinking: { type: "disabled" } })
  })

  it("can require a tool call", async () => {
    const client = new CaptureClient()
    const provider = new ResponsesProvider("model", { client: client as never })
    const tools = new ToolRegistry()
    tools.register(new EchoTool())
    const it = provider.stream(new AgentConfig(200, 4096, 0.1, "code-agent", "required"), tools, [], new AbortController().signal)
    await it.next()
    expect(client.kwargs["tool_choice"]).toBe("required")
  })

  it("deepseek stream injects schema and uses json_object", async () => {
    const client = new CaptureClient()
    const provider = new DeepSeekProvider("model", { client: client as never })
    const events = []
    for await (const e of provider.stream(new AgentConfig(), new ToolRegistry(), [], new AbortController().signal, { outputType: structuredOut })) {
      events.push(e)
    }
    expect(client.kwargs["response_format"]).toEqual({ type: "json_object" })
    const msgs = client.kwargs["messages"] as Array<{ role: string; content: string }>
    expect(msgs[msgs.length - 1]!.role).toBe("system")
    expect(msgs[msgs.length - 1]!.content).toContain("Return a JSON object matching this schema")
    expect(events.some((e) => e.kind === "response_completed")).toBe(true)
  })
})

describe("structured output", () => {
  let tmp: string
  afterEach(() => {
    if (tmp) rmSync(tmp, { recursive: true, force: true })
  })

  it("validates retries and persists", async () => {
    class StructuredProvider {
      calls = 0
      async *stream() {
        this.calls += 1
        if (this.calls === 1) {
          yield new StreamEvent("text_delta", { delta: "not json", accumulated: "not json" })
        } else {
          yield new StreamEvent("text_delta", { delta: '{"answer":"hi"}', accumulated: '{"answer":"hi"}' })
        }
        yield new StreamEvent("response_completed")
      }
    }
    tmp = mkdtempSync(join(tmpdir(), "athena-art-"))
    const store = new LocalArtifactStore(join(tmp, "artifacts"))
    const tools = new ToolRegistry()
    const agent = new Agent(new ResponsesProvider("model", { client: {} as never }), tools, "system", null, {
      outputType: structuredOut,
      artifacts: store,
    })
    agent.model = new StructuredProvider() as never
    const outcome = await agent.run(ctx(tools))
    expect(outcome.resultRef.startsWith("sha256:")).toBe(true)
    expect(await store.getText(outcome.resultRef)).toBe('{"answer":"hi"}')
  })

  it("raises after max retries", async () => {
    class AlwaysInvalidProvider {
      calls = 0
      async *stream() {
        this.calls += 1
        yield new StreamEvent("text_delta", { delta: "not json", accumulated: "not json" })
        yield new StreamEvent("response_completed")
      }
    }
    const tools = new ToolRegistry()
    const agent = new Agent(new ResponsesProvider("model", { client: {} as never }), tools, "system", null, {
      outputType: structuredOut,
    })
    const provider = new AlwaysInvalidProvider()
    agent.model = provider as never
    await expect(agent.run(ctx(tools))).rejects.toThrow(/structured output invalid/)
    expect(provider.calls).toBe(4)
  })

  it("without artifacts uses virtual ref", async () => {
    class ValidProvider {
      async *stream() {
        yield new StreamEvent("text_delta", { delta: '{"answer":"hi"}', accumulated: '{"answer":"hi"}' })
        yield new StreamEvent("response_completed")
      }
    }
    const tools = new ToolRegistry()
    const agent = new Agent(new ResponsesProvider("model", { client: {} as never }), tools, "system", null, {
      outputType: structuredOut,
    })
    agent.model = new ValidProvider() as never
    const outcome = await agent.run(ctx(tools))
    expect(outcome.resultRef.startsWith("result://")).toBe(true)
    expect(outcome.resultRef).toBe("result://t1.1")
  })
})

describe("ask_user tool", () => {
  class AskUserProvider {
    calls = 0
    async *stream() {
      this.calls += 1
      if (this.calls === 1) {
        yield new StreamEvent("function_call", {
          call_id: "c1",
          name: "request_user_input",
          arguments: { prompt: "请选择方案" },
        })
      } else {
        yield new StreamEvent("text_delta", { delta: "好的，采用方案A", accumulated: "好的，采用方案A" })
      }
      yield new StreamEvent("response_completed")
    }
  }

  it("round trips answer into memory", async () => {
    const asked: string[] = []
    const askUser: AskUser = async (prompt: string) => {
      asked.push(prompt)
      return "方案A"
    }
    const tools = new ToolRegistry()
    tools.register(new RequestUserInputTool())
    const agent = new Agent(new ResponsesProvider("model", { client: {} as never }), tools, "system")
    agent.model = new AskUserProvider() as never
    const memory = new ContextManager()
    const outcome = await agent.run(ctx(tools, { memory, askUser }))
    expect(asked).toEqual(["请选择方案"])
    const returns = memory.items.flatMap((m) => m.parts).filter((p) => p.part_kind === "tool-return")
    expect(returns.length).toBe(1)
    expect("content" in returns[0]! ? returns[0]!.content : "").toBe("方案A")
    expect(outcome.resultRef).toBe("result://t1.1")
  })

  it("without injection reports error", async () => {
    const tools = new ToolRegistry()
    tools.register(new RequestUserInputTool())
    const agent = new Agent(new ResponsesProvider("model", { client: {} as never }), tools, "system")
    agent.model = new AskUserProvider() as never
    const memory = new ContextManager()
    const outcome = await agent.run(ctx(tools, { memory }))
    const returns = memory.items.flatMap((m) => m.parts).filter((p) => p.part_kind === "tool-return")
    expect(returns.length).toBe(1)
    const content = "content" in returns[0]! ? returns[0]!.content : ""
    expect(content).toContain("[ERROR]")
    expect(content).toContain("ask_user")
    expect(outcome.resultRef).toBe("result://t1.1")
  })

  it("agent_runner binds ask_user factory", async () => {
    const bound: Array<[string, string]> = []
    const makeAskUser = (_t: AthenaThread, _u: AthenaTurn): AskUser => {
      return async () => {
        bound.push([_t.thread_id, _u.turn_id])
        return "ok"
      }
    }
    const tools = new ToolRegistry()
    tools.register(new RequestUserInputTool())
    const agent = new Agent(new ResponsesProvider("model", { client: {} as never }), tools, "system")
    agent.model = new AskUserProvider() as never
    const runner = agentRunner(agent, tools, { askUser: makeAskUser })
    const outcome = await runner(thread(), turn(), noopEmit)
    expect(bound).toEqual([["t1", "t1.1"]])
    expect(outcome.resultRef).toBe("result://t1.1")
  })
})

describe("create_provider", () => {
  const envKeys = ["LLM_PROVIDER"]
  let restore: (() => void) | null = null
  afterEach(() => {
    restore?.()
    restore = null
  })

  it("routes by LLM_PROVIDER env", () => {
    const saved: Record<string, string | undefined> = {}
    for (const k of envKeys) {
      saved[k] = process.env[k]
    }
    restore = () => {
      for (const k of envKeys) {
        if (saved[k] === undefined) delete process.env[k]
        else process.env[k] = saved[k]
      }
    }

    process.env.LLM_PROVIDER = "deepseek"
    expect(createProvider("m")).toBeInstanceOf(DeepSeekProvider)

    process.env.LLM_PROVIDER = "openai"
    expect(createProvider("m")).toBeInstanceOf(OpenAIProvider)

    process.env.LLM_PROVIDER = "anthropic"
    expect(() => createProvider("m")).toThrow(/LLM_PROVIDER/)

    process.env.LLM_PROVIDER = "bogus"
    expect(() => createProvider("m")).toThrow(/LLM_PROVIDER/)
  })
})

describe("AnthropicProvider", () => {
  it("raises on construction", () => {
    expect(() => new AnthropicProvider("m")).toThrow(/LLM_PROVIDER/)
  })
})
