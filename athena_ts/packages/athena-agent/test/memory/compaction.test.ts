import { describe, expect, it } from "vitest"
import { modelRequest, modelResponse, textPart, userPrompt } from "../../src/messages.js"
import { Compactor, type SummarizeLLM } from "../../src/memory/compaction.js"
import { ContextManager } from "../../src/memory/context-manager.js"

function user(text: string) {
  return modelRequest([userPrompt(text)])
}

function assistant(text: string) {
  return modelResponse([textPart(text)])
}

class FakeLLM implements SummarizeLLM {
  calls: Array<Record<string, unknown>> = []
  messages = {
    create: async (kwargs: Record<string, unknown>) => {
      this.calls.push(kwargs)
      return { content: [{ text: "compact summary" }] }
    },
  }
}

describe("Compactor", () => {
  it("should_compact uses configured threshold", () => {
    const ctx = new ContextManager()
    ctx.append(user("x".repeat(100)))
    const compactor = new Compactor()
    expect(compactor.shouldCompact(ctx, ctx.tokens)).toBe(true)
    expect(compactor.shouldCompact(ctx, ctx.tokens + 1)).toBe(false)
  })

  it("compact replaces old history without duplicating tail", async () => {
    const ctx = new ContextManager()
    ctx.append(user("first ".repeat(100)))
    ctx.append(assistant("second ".repeat(100)))
    const tail = user("recent")
    ctx.append(tail)
    const beforeVersion = ctx.version
    const llm = new FakeLLM()

    const checkpoint = await new Compactor(1).compact(ctx, llm)

    expect(checkpoint.summary).toBe("compact summary")
    expect(checkpoint.originalItems.length).toBe(2)
    expect(
      "content" in checkpoint.originalItems[0]!.parts[0]!
        ? checkpoint.originalItems[0]!.parts[0]!.content.startsWith("first")
        : false
    ).toBe(true)
    expect(
      "content" in checkpoint.originalItems[1]!.parts[0]!
        ? checkpoint.originalItems[1]!.parts[0]!.content.startsWith("second")
        : false
    ).toBe(true)
    expect(ctx.items.length).toBe(2)
    const lastContent =
      "content" in ctx.items[ctx.items.length - 1]!.parts[0]!
        ? ctx.items[ctx.items.length - 1]!.parts[0]!.content
        : ""
    expect(lastContent).toBe("recent")
    const tailCount = ctx.items.filter(
      (item) => "content" in item.parts[0]! && item.parts[0]!.content === "recent"
    ).length
    expect(tailCount).toBe(1)
    expect(ctx.version).toBe(beforeVersion + 1)
    const prompt = (llm.calls[0]!["messages"] as Array<{ content: string }>)[0]!.content
    expect(prompt).toContain("first")
    expect(prompt).toContain("second")
    expect(prompt).not.toContain("recent")
  })

  it("compact with nothing old is a noop", async () => {
    const ctx = new ContextManager()
    const only = user("only")
    ctx.append(only)
    const llm = new FakeLLM()
    const checkpoint = await new Compactor(10_000).compact(ctx, llm)

    expect(checkpoint.originalItems).toEqual([])
    expect(checkpoint.summary).toBe("")
    expect(ctx.items).toEqual([only])
    expect(llm.calls).toEqual([])
  })
})
