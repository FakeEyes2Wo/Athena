import { appendFileSync, existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { basename, join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import {
  ModelMessagesTypeAdapter,
  modelRequest,
  modelResponse,
  textPart,
  toolCallPart,
  toolReturnPart,
  userPrompt,
} from "../../src/messages.js"
import { resumeContext, resumeContextSync, RolloutRecorder } from "../../src/memory/rollout.js"

function user(text: string) {
  return modelRequest([userPrompt(text)])
}

function assistant(text: string) {
  return modelResponse([textPart(text)])
}

function toolResult(name: string, content: string) {
  return modelRequest([toolReturnPart(name, content, "c1")])
}

function contentOf(part: unknown): string {
  return typeof part === "object" && part !== null && "content" in part && typeof (part as { content: unknown }).content === "string"
    ? (part as { content: string }).content
    : ""
}

let tmp: string

afterEach(() => {
  if (tmp) rmSync(tmp, { recursive: true, force: true })
})

function tmpProject(): string {
  tmp = mkdtempSync(join(tmpdir(), "athena-rollout-"))
  return tmp
}

describe("RolloutRecorder", () => {
  it("open creates file in date hierarchy", async () => {
    const rec = new RolloutRecorder(tmpProject())
    const path = await rec.open("thread-abc123")
    await rec.close()
    expect(existsSync(path)).toBe(true)
    expect(path).toContain(".athena")
    expect(path.endsWith(".jsonl")).toBe(true)
  })

  it("record writes jsonl line", async () => {
    const rec = new RolloutRecorder(tmpProject())
    await rec.open("t1")
    rec.record(user("hello"))
    await rec.close()
    const lines = readFileSync(rec.path!, "utf8").trim().split("\n")
    expect(lines.length).toBe(1)
    const data = JSON.parse(lines[0])
    expect(data["seq"]).toBe(0)
    expect("msg" in data).toBe(true)
  })

  it("multiple records increment seq", async () => {
    const rec = new RolloutRecorder(tmpProject())
    await rec.open("t2")
    rec.record(user("a"))
    rec.record(assistant("b"))
    rec.record(user("c"))
    await rec.close()
    const lines = readFileSync(rec.path!, "utf8").trim().split("\n")
    expect(lines.length).toBe(3)
    const seqs = lines.map((l: string) => JSON.parse(l)["seq"])
    expect(seqs).toEqual([0, 1, 2])
  })

  it("record compaction writes marker", async () => {
    const rec = new RolloutRecorder(tmpProject())
    await rec.open("t3")
    rec.recordCompaction(5, "summary text")
    await rec.close()
    const data = JSON.parse(readFileSync(rec.path!, "utf8").trim())
    expect(data["type"]).toBe("compaction")
    expect(data["version"]).toBe(5)
    expect(data["summary"]).toBe("summary text")
  })

  it("record before open is noop", async () => {
    const rec = new RolloutRecorder(tmpProject())
    rec.record(user("nope"))
    expect(rec.path).toBeNull()
  })

  it("open is idempotent", async () => {
    const rec = new RolloutRecorder(tmpProject())
    const p1 = await rec.open("t4")
    const p2 = await rec.open("t4")
    rec.record(user("still same rollout"))
    await rec.close()
    expect(p1).toBe(p2)
    expect(readFileSync(p1, "utf8").split("\n").filter(Boolean).length).toBe(1)
  })

  it("append after truncated tail preserves new complete record", async () => {
    const root = tmpProject()
    const path = join(root, "agent.jsonl")
    const first = new RolloutRecorder(root)
    first.openSync("stable-agent", path)
    first.record(user("complete-before-crash"))
    await first.close()
    appendFileSync(path, '{"seq":1,"msg":')

    const resumed = new RolloutRecorder(root)
    resumed.openSync("stable-agent", path)
    resumed.record(user("complete-after-restart"))
    await resumed.close()

    const context = resumeContextSync(path)
    expect(context.items.map((item) => contentOf(item.parts[0]))).toEqual([
      "complete-before-crash",
      "complete-after-restart",
    ])
  })

  it("thread id cannot create nested paths", async () => {
    const rec = new RolloutRecorder(tmpProject())
    const path = await rec.open("../bad/name")
    await rec.close()
    expect(existsSync(path)).toBe(true)
    const base = basename(path)
    expect(base.includes("/")).toBe(false)
    expect(base.includes("\\")).toBe(false)
  })

  it("roundtrip messages are valid json", async () => {
    const rec = new RolloutRecorder(tmpProject())
    await rec.open("t5")
    const msg = modelResponse([textPart("result"), toolCallPart("bash", { cmd: "ls" })])
    rec.record(msg)
    rec.record(toolResult("bash", "file1\nfile2"))
    await rec.close()

    for (const line of readFileSync(rec.path!, "utf8").trim().split("\n")) {
      const data = JSON.parse(line)
      const msgs = ModelMessagesTypeAdapter.parse(data["msg"])
      expect(msgs.length).toBe(1)
    }
  })
})

describe("resumeContext", () => {
  it("empty file yields empty context", async () => {
    const rec = new RolloutRecorder(tmpProject())
    await rec.open("r1")
    await rec.close()
    const ctx = await resumeContext(rec.path!)
    expect(ctx.tokens).toBe(0)
    expect(ctx.items).toEqual([])
  })

  it("replays messages after compaction", async () => {
    const rec = new RolloutRecorder(tmpProject())
    await rec.open("r2")
    rec.record(user("early question"))
    rec.record(assistant("early answer"))
    rec.recordCompaction(1, "User asked about X, assistant explained Y.")
    rec.record(user("follow-up"))
    rec.record(assistant("follow-up answer"))
    await rec.close()

    const ctx = await resumeContext(rec.path!)
    expect(ctx.tokens).toBeGreaterThan(0)
    const first = contentOf(ctx.items[0]!.parts[0]!)
    expect(first).toContain("HISTORY SUMMARY")
    const last = contentOf(ctx.items[ctx.items.length - 1]!.parts[0]!)
    expect(last).toBe("follow-up answer")
  })

  it("no compaction replays all", async () => {
    const rec = new RolloutRecorder(tmpProject())
    await rec.open("r3")
    rec.record(user("q1"))
    rec.record(assistant("a1"))
    rec.record(user("q2"))
    await rec.close()

    const ctx = await resumeContext(rec.path!)
    expect(ctx.items.length).toBe(3)
  })

  it("uses latest compaction only", async () => {
    const rec = new RolloutRecorder(tmpProject())
    await rec.open("r4")
    rec.record(user("q1"))
    rec.recordCompaction(1, "first compaction")
    rec.record(user("q2"))
    rec.recordCompaction(2, "second compaction")
    rec.record(user("q3"))
    await rec.close()

    const ctx = await resumeContext(rec.path!)
    expect(ctx.items.length).toBe(2)
    expect(contentOf(ctx.items[0]!.parts[0]!)).toContain("second compaction")
    expect(contentOf(ctx.items[1]!.parts[0]!)).toBe("q3")
  })

  it("skips torn final jsonl record", async () => {
    const rec = new RolloutRecorder(tmpProject())
    await rec.open("r5")
    rec.record(user("durable"))
    await rec.close()
    appendFileSync(rec.path!, '{"seq":1,"msg":')

    const ctx = await resumeContext(rec.path!)
    expect(ctx.items.length).toBe(1)
    expect(contentOf(ctx.items[0]!.parts[0]!)).toBe("durable")
  })
})
