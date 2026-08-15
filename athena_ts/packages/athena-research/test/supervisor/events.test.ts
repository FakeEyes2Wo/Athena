import { describe, expect, it } from "vitest"
import { EventProjector, redact, sanitizeTerminalText, truncateMiddle } from "../../src/supervisor/events.js"

describe("redact", () => {
  it("masks key=value secrets", () => {
    expect(redact("token api_key=secret123 for service")).toContain("[REDACTED]")
    expect(redact("token api_key=secret123 for service")).not.toContain("secret123")
  })

  it("masks sk- tokens", () => {
    expect(redact("use sk-abcdefgh12345678 to auth")).toContain("[REDACTED]")
  })
})

describe("sanitizeTerminalText", () => {
  it("strips ansi sequences and unprintables", () => {
    expect(sanitizeTerminalText("\x1b[31mred\x1b[0m text")).toBe("red text")
  })

  it("normalizes line endings", () => {
    expect(sanitizeTerminalText("a\r\nb\rc")).toBe("a\nb\nc")
  })
})

describe("truncateMiddle", () => {
  it("keeps short text intact", () => {
    expect(truncateMiddle("short", 100)).toBe("short")
  })

  it("middle-truncates long text", () => {
    const out = truncateMiddle("x".repeat(100), 20)
    expect(out.length).toBeLessThan(100)
    expect(out).toContain("chars truncated")
    expect(out.startsWith("xxxxxxxxxx")).toBe(true)
  })
})

describe("EventProjector", () => {
  it("projects redacted output with monotonic seq", () => {
    const projector = new EventProjector({ putText: async () => "ref" })
    const first = projector.output({ source: "supervisor", channel: "text", text: "api_key=secret123" })
    const second = projector.output({ source: "agent", channel: "text", text: "hello" })
    expect(first.seq).toBe(1)
    expect(second.seq).toBe(2)
    expect(first.text).toContain("[REDACTED]")
  })

  it("resume skips past replayed history", () => {
    const projector = new EventProjector({ putText: async () => "ref" })
    projector.resume(5)
    expect(projector.output({ source: "agent", channel: "text", text: "x" }).seq).toBe(6)
  })
})
