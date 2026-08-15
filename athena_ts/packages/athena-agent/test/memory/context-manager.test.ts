import { describe, expect, it } from "vitest"
import {
  modelRequest,
  modelResponse,
  systemPrompt,
  textPart,
  toolCallPart,
  toolReturnPart,
  userPrompt,
} from "../../src/messages.js"
import { ContextManager } from "../../src/memory/context-manager.js"

function user(text: string) {
  return modelRequest([userPrompt(text)])
}

function assistant(text: string) {
  return modelResponse([textPart(text)])
}

function toolCall(name: string, args: Record<string, unknown>) {
  return modelResponse([toolCallPart(name, args)])
}

function toolResult(name: string, content: string, callId = "c1") {
  return modelRequest([toolReturnPart(name, content, callId)])
}

describe("ContextManager", () => {
  it("empty context has zero tokens", () => {
    const ctx = new ContextManager()
    expect(ctx.tokens).toBe(0)
    expect(ctx.items).toEqual([])
    expect(ctx.version).toBe(0)
  })

  it("append increments tokens and version", () => {
    const ctx = new ContextManager()
    ctx.append(user("hello world"))
    expect(ctx.tokens).toBeGreaterThan(0)
    expect(ctx.version).toBe(1)
    expect(ctx.items.length).toBe(1)
  })

  it("append multiple accrues tokens", () => {
    const ctx = new ContextManager()
    ctx.append(user("hello"))
    const t1 = ctx.tokens
    ctx.append(assistant("hi there"))
    expect(ctx.tokens).toBeGreaterThan(t1)
    expect(ctx.version).toBe(2)
  })

  it("short message still costs tokens", () => {
    const ctx = new ContextManager()
    ctx.append(user("x"))
    expect(ctx.tokens).toBeGreaterThan(0)
  })

  it("items returns copy", () => {
    const ctx = new ContextManager()
    ctx.append(user("a"))
    const copy = ctx.items
    copy.splice(0)
    expect(ctx.items.length).toBe(1)
  })

  it("large tool result is truncated", () => {
    const ctx = new ContextManager()
    ctx.append(toolResult("bash", "x".repeat(100_000)))
    const part = ctx.items[0]!.parts[0]!
    expect("content" in part && typeof part.content === "string").toBe(true)
    const c = "content" in part ? part.content : ""
    expect(c.length).toBeLessThan(100_000)
    expect(c).toContain("[TRUNCATED]")
  })

  it("truncation preserves head and tail without mutating input", () => {
    const originalContent = "BEGIN" + "x".repeat(60_000) + "END"
    const msg = toolResult("bash", originalContent)
    const ctx = new ContextManager()
    ctx.append(msg)

    const stored = "content" in ctx.items[0]!.parts[0]! ? ctx.items[0]!.parts[0]!.content : ""
    expect(stored.startsWith("BEGIN")).toBe(true)
    expect(stored.endsWith("END")).toBe(true)
    expect(stored.length).toBeLessThanOrEqual(50_000)
    expect("content" in msg.parts[0]! ? msg.parts[0]!.content : "").toBe(originalContent)
  })

  it("tool result under limit stays intact", () => {
    const ctx = new ContextManager()
    ctx.append(toolResult("bash", "short output"))
    const c = "content" in ctx.items[0]!.parts[0]! ? ctx.items[0]!.parts[0]!.content : ""
    expect(c).toBe("short output")
  })

  it("system prompt not truncated", () => {
    const ctx = new ContextManager()
    const huge = "y".repeat(60_000)
    ctx.append(modelRequest([systemPrompt(huge)]))
    const c = "content" in ctx.items[0]!.parts[0]! ? ctx.items[0]!.parts[0]!.content : ""
    expect(c.length).toBe(60_000)
  })

  it("token margin positive when under limit", () => {
    const ctx = new ContextManager(200_000)
    expect(ctx.tokenMargin()).toBe(Math.floor(200_000 * 0.85))
  })

  it("token margin decreases after appends", () => {
    const ctx = new ContextManager(200_000)
    const m1 = ctx.tokenMargin()
    ctx.append(user("hello world ".repeat(100)))
    expect(ctx.tokenMargin()).toBeLessThan(m1)
  })
})

describe("replaceRange", () => {
  it("replace keeps token count accurate", () => {
    const ctx = new ContextManager()
    ctx.append(user("hello world"))
    ctx.append(assistant("hi back"))
    const before = ctx.tokens
    ctx.replaceRange(0, 2, [modelRequest([systemPrompt("summary")])])
    expect(ctx.tokens).toBeLessThan(before)
    expect(ctx.items.length).toBe(1)
    expect(ctx.version).toBe(3)
  })

  it("replace empty range", () => {
    const ctx = new ContextManager()
    ctx.append(user("a"))
    const before = ctx.tokens
    const v0 = ctx.version
    ctx.replaceRange(0, 0, [])
    expect(ctx.tokens).toBe(before)
    expect(ctx.version).toBe(v0 + 1)
  })

  it("replace partial range keeps tail", () => {
    const ctx = new ContextManager()
    for (const text of ["first", "second", "third"]) ctx.append(user(text))
    ctx.replaceRange(0, 2, [user("merged")])
    expect(ctx.items.length).toBe(2)
    const tail = "content" in ctx.items[1]!.parts[0]! ? ctx.items[1]!.parts[0]!.content : ""
    expect(tail).toBe("third")
  })

  it("version monotonic across operations", () => {
    const ctx = new ContextManager()
    const v0 = ctx.version
    ctx.append(user("a"))
    const v1 = ctx.version
    ctx.replaceRange(0, 1, [assistant("b")])
    const v2 = ctx.version
    expect(v0).toBeLessThan(v1)
    expect(v1).toBeLessThan(v2)
  })

  it("replacement applies same tool output limit", () => {
    const ctx = new ContextManager()
    ctx.append(user("old"))
    const replacement = toolResult("bash", "z".repeat(80_000))
    ctx.replaceRange(0, 1, [replacement])
    const stored = "content" in ctx.items[0]!.parts[0]! ? ctx.items[0]!.parts[0]!.content : ""
    expect(stored.length).toBeLessThanOrEqual(50_000)
    expect(stored).toContain("[TRUNCATED]")
    expect("content" in replacement.parts[0]! ? replacement.parts[0]!.content : "").toBe("z".repeat(80_000))
  })
})

describe("token estimation", () => {
  it("english text estimate is plausible", () => {
    const ctx = new ContextManager()
    ctx.append(user("hello world ".repeat(36)))
    expect(ctx.tokens).toBeGreaterThanOrEqual(80)
    expect(ctx.tokens).toBeLessThanOrEqual(120)
  })

  it("code block estimate is plausible", () => {
    const ctx = new ContextManager()
    ctx.append(assistant("def foo():\n    return 42\n".repeat(50)))
    expect(ctx.tokens).toBeGreaterThan(0)
  })
})

describe("edge cases", () => {
  it("context limit is stored", () => {
    expect(new ContextManager(128_000).limit).toBe(128_000)
  })

  it("tool call arguments contribute tokens", () => {
    const ctx = new ContextManager()
    ctx.append(toolCall("bash", { command: "ls" }))
    expect(ctx.tokens).toBeGreaterThan(0)
  })

  it("mixed parts token sum", () => {
    const ctx = new ContextManager()
    ctx.append(
      modelResponse([textPart("Let me check."), toolCallPart("bash", { command: "ls" })])
    )
    expect(ctx.tokens).toBeGreaterThan(0)
  })
})
