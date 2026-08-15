import { describe, expect, it } from "vitest"
import { CancelledError, CancellationToken } from "../src/cancel.js"
import { BaseTool, ToolRegistry, tool } from "../src/tool.js"
import {
  TOOL_BEGIN,
  TOOL_END,
  TOOL_ERROR,
  ToolContext,
  ToolResult,
  ToolSpec,
  type EmitEvent,
} from "../src/tool-types.js"

class EchoTool extends BaseTool {
  readonly spec = new ToolSpec("echo", "echo", {})
  execute(input: Record<string, unknown>): Record<string, unknown> {
    return input
  }
}

class FailingTool extends BaseTool {
  readonly spec = new ToolSpec("fail", "fail", {})
  execute(): never {
    throw new Error("boom")
  }
}

class CancellingTool extends BaseTool {
  readonly spec = new ToolSpec("cancel_me", "cancel", {})
  execute(): never {
    throw new CancelledError()
  }
}

class ToolResultTool extends BaseTool {
  readonly spec = new ToolSpec("direct", "direct", {})
  execute(): ToolResult {
    return new ToolResult({ custom: true })
  }
}

const decoEcho = tool(
  async (input: Record<string, unknown>) => ({
    text: input["text"],
    repeat: input["repeat"] ?? 1,
  }),
  { name: "deco_echo", description: "使用 @tool 装饰器的 Echo。" }
)

const decoFail = tool(() => {
  throw new Error("bad input")
}, { name: "deco_fail", description: "始终失败。" })

async function collectEvents(
  t: BaseTool,
  input: Record<string, unknown>
): Promise<{ result: ToolResult; events: string[] }> {
  const events: string[] = []
  const emit: EmitEvent = async (kind: string) => {
    events.push(kind)
  }
  const ctx = new ToolContext(t.spec.name, "test-1", emit, new CancellationToken())
  const result = await t.ainvoke(ctx, input)
  return { result, events }
}

describe("BaseTool", () => {
  it("sync invoke", async () => {
    const result = await new EchoTool().invoke({ x: 1, y: "hello" })
    expect(result.success).toBe(true)
    expect(result.data).toEqual({ x: 1, y: "hello" })
  })

  it("ainvoke events", async () => {
    const { result, events } = await collectEvents(new EchoTool(), { a: 1 })
    expect(result.success).toBe(true)
    expect(result.data).toEqual({ a: 1 })
    expect(events).toContain(TOOL_BEGIN)
    expect(events).toContain(TOOL_END)
  })

  it("ainvoke error wrapped", async () => {
    const { result, events } = await collectEvents(new FailingTool(), {})
    expect(result.success).toBe(false)
    expect(result.error).toContain("boom")
    expect(events).toContain(TOOL_BEGIN)
    expect(events).toContain(TOOL_ERROR)
  })

  it("ainvoke cancelled error", async () => {
    await expect(collectEvents(new CancellingTool(), {})).rejects.toBeInstanceOf(
      CancelledError
    )
  })

  it("direct tool result passthrough", async () => {
    const { result } = await collectEvents(new ToolResultTool(), {})
    expect(result.success).toBe(true)
    expect(result.data).toEqual({ custom: true })
  })
})

describe("tool() factory", () => {
  it("decorated invoke", async () => {
    const result = await decoEcho.invoke({ text: "hi" })
    expect(result.success).toBe(true)
    expect(result.data).toEqual({ text: "hi", repeat: 1 })
  })

  it("decorated ainvoke", async () => {
    const { result, events } = await collectEvents(decoEcho, { text: "x", repeat: 3 })
    expect(result.data).toEqual({ text: "x", repeat: 3 })
    expect(events).toContain(TOOL_BEGIN)
    expect(events).toContain(TOOL_END)
  })

  it("decorated spec", () => {
    expect(decoEcho.spec.name).toBe("deco_echo")
    expect(decoEcho.spec.description).toBe("使用 @tool 装饰器的 Echo。")
  })

  it("decorated error", async () => {
    const { result, events } = await collectEvents(decoFail, {})
    expect(result.success).toBe(false)
    expect(result.error).toContain("bad input")
    expect(events).toContain(TOOL_ERROR)
  })
})

describe("ToolRegistry", () => {
  it("register and resolve", () => {
    const reg = new ToolRegistry()
    reg.register(new EchoTool())
    expect(reg.has("echo")).toBe(true)
    expect(reg.size).toBe(1)
  })

  it("register duplicate", () => {
    const reg = new ToolRegistry()
    reg.register(new EchoTool())
    expect(() => reg.register(new EchoTool())).toThrow(/already registered/)
  })

  it("resolve missing", () => {
    expect(() => new ToolRegistry().resolve("nope")).toThrow(/not found/)
  })

  it("specs sorted", () => {
    const reg = new ToolRegistry()
    reg.register(new EchoTool())
    reg.register(decoEcho)
    expect(reg.specs.map((s) => s.name)).toEqual(["deco_echo", "echo"])
  })
})
