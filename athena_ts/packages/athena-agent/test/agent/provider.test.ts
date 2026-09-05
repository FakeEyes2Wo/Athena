import { describe, expect, it } from "vitest"
import { AgentConfig } from "../../src/agent/models.js"
import { DeepSeekTextFilter, ResponsesProvider, StreamEvent } from "../../src/agent/provider.js"
import { ToolRegistry } from "../../src/tool.js"

function filtered(chunks: string[]): string {
  const f = new DeepSeekTextFilter()
  const out: string[] = []
  for (const chunk of chunks) out.push(f.push(chunk))
  out.push(f.flush())
  return out.join("")
}

function chunk(
  content: string | null = null,
  toolCalls: unknown = null,
  finishReason: string | null = null
) {
  return {
    choices: [{ delta: { content, tool_calls: toolCalls }, finish_reason: finishReason }],
  }
}

function streamClient(chunks: unknown[]) {
  return {
    chat: {
      completions: {
        create: async () => {
          return (async function* () {
            for (const c of chunks) yield c
          })()
        },
      },
    },
  }
}

const cases: Array<[string[], string]> = [
  [
    ["before <｜DSML｜tool_use_error><tool_name>write</tool_name></｜DSML｜tool_use_error> after"],
    "before  after",
  ],
  [
    ["before ", "<｜DS", "ML｜tool_calls>body</｜DSML｜tool_calls>", " after"],
    "before  after",
  ],
  [["<|DSML|tool_call>read</|DSML|tool_call> visible"], " visible"],
  [["<|DS", "ML|tool_call>read\n", "</|DSML|tool_calls>"], ""],
  [["visible <｜DSML｜tool_calls>partial body, no close"], "visible "],
  [
    [
      "a<｜DSML｜tool_use_error>x</｜DSML｜tool_use_error>b<｜DSML｜function_calls>y</｜DSML｜function_calls>c",
    ],
    "abc",
  ],
]

describe("DeepSeekTextFilter", () => {
  it.each(cases)("drops markers for %j", (chunks, expected) => {
    expect(filtered(chunks)).toBe(expected)
    expect(filtered(chunks)).not.toContain("DSML")
  })

  it("holds partial open token", () => {
    const f = new DeepSeekTextFilter()
    const mid = f.push("safe text<｜DSM")
    expect(mid).not.toContain("<｜DSM")
    expect(f.push("L｜tool_calls>body</｜DSML｜tool_calls> done") + f.flush()).toBe(
      "safe text done"
    )
  })

  it("preserves clean json", () => {
    const f = new DeepSeekTextFilter()
    const chunks = [
      '{"decision":"submit",',
      '"reason":"ok"}<｜DSML｜tool_calls>',
      "shadow</｜DSML｜tool_calls>",
    ]
    const out: string[] = []
    for (const c of chunks) out.push(f.push(c))
    out.push(f.flush())
    expect(out.join("")).toBe('{"decision":"submit","reason":"ok"}')
  })
})

describe("ResponsesProvider streaming", () => {
  it("observes native cancellation between chunks without publishing later text", async () => {
    const controller = new AbortController()
    const provider = new ResponsesProvider("test-model", {
      client: streamClient([chunk("first"), chunk("later"), chunk(null, null, "stop")]),
      providerKind: "openai",
    })
    const stream = provider.stream(new AgentConfig(), new ToolRegistry(), [], controller.signal)
    const first = await stream.next()
    expect(first.value?.kind).toBe("text_delta")
    expect(first.value?.data["delta"]).toBe("first")
    controller.abort()
    controller.abort()
    const remaining: StreamEvent[] = []
    for await (const event of stream) remaining.push(event)
    expect(remaining.map((event) => [event.kind, event.data])).toEqual([
      ["error", { message: "cancelled" }],
    ])
  })

  it("deepseek stream strips dsml from accumulated", async () => {
    const client = streamClient([
      chunk('{"decision":"submit",'),
      chunk('  "reason":"ok"}<｜DSML｜tool_calls>body</｜DSML｜tool_calls>', null, "stop"),
    ])
    const provider = new ResponsesProvider("deepseek-test", {
      client: client as never,
      providerKind: "deepseek",
    })

    const events: StreamEvent[] = []
    for await (const event of provider.stream(
      new AgentConfig(200, 512, 0.0, "code-agent", "auto"),
      new ToolRegistry(),
      [],
      new AbortController().signal
    )) {
      events.push(event)
    }

    const accumulated = events
      .filter((e) => e.kind === "text_delta")
      .map((e) => e.data["accumulated"] as string)
    const final = events.find((e) => e.kind === "response_completed")!
    expect(accumulated.join("")).not.toContain("DSML")
    expect(final.data["accumulated_text"] as string).not.toContain("DSML")
    expect((final.data["accumulated_text"] as string).startsWith('{"decision":"submit"')).toBe(
      true
    )
  })

  it("openai stream is untouched by dsml filter", async () => {
    const client = streamClient([chunk('{"a": 1}', null, "stop")])
    const provider = new ResponsesProvider("openai-test", {
      client: client as never,
      providerKind: "openai",
    })

    const events: StreamEvent[] = []
    for await (const event of provider.stream(
      new AgentConfig(200, 512, 0.0, "code-agent", "auto"),
      new ToolRegistry(),
      [],
      new AbortController().signal
    )) {
      events.push(event)
    }

    const final = events.find((e) => e.kind === "response_completed")!
    expect(final.data["accumulated_text"]).toBe('{"a": 1}')
  })
})
