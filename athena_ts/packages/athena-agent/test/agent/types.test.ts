import { describe, expect, it } from "vitest"
import {
  TERMINAL_RUN_STATUSES,
  AgentBusyError,
  AgentCommandError,
  AgentError,
  AgentMessage,
  AgentRunFailed,
  AgentRunInterrupted,
  AgentSpec,
  AgentStatus,
  ErrorCode,
  JsonCodec,
  ReturnWhen,
  RunStatus,
  type AgentRunner,
  type EventSink,
} from "../../src/agent/types.js"

class EchoRunner implements AgentRunner {
  async run(request: unknown, _session: unknown, _emit: EventSink): Promise<unknown> {
    return { echo: request }
  }
}

describe("agent types", () => {
  it("run status enum values match spec", () => {
    expect(Object.values(RunStatus)).toEqual([
      "queued",
      "running",
      "completed",
      "failed",
      "interrupted",
    ])
  })

  it("agent status enum values match spec", () => {
    expect(Object.values(AgentStatus)).toEqual([
      "starting",
      "idle",
      "running",
      "waiting",
      "waiting_for_human",
      "error",
      "closed",
    ])
  })

  it("terminal set contains only run terminal statuses", () => {
    expect(TERMINAL_RUN_STATUSES.size).toBe(3)
    expect(TERMINAL_RUN_STATUSES.has(RunStatus.COMPLETED)).toBe(true)
    expect(TERMINAL_RUN_STATUSES.has(RunStatus.FAILED)).toBe(true)
    expect(TERMINAL_RUN_STATUSES.has(RunStatus.INTERRUPTED)).toBe(true)
  })

  it("interrupted is run terminal but not agent terminal", () => {
    expect(TERMINAL_RUN_STATUSES.has(RunStatus.INTERRUPTED)).toBe(true)
    expect(TERMINAL_RUN_STATUSES.has(AgentStatus.CLOSED as unknown as RunStatus)).toBe(false)
  })

  it("return when has both modes", () => {
    expect(new Set(Object.values(ReturnWhen))).toEqual(
      new Set(["first_completed", "all_completed"])
    )
  })

  it("agent message marks control messages", () => {
    expect(new AgentMessage(null, "control").isControl).toBe(true)
    expect(new AgentMessage("root", "hi").isControl).toBe(false)
  })

  it("agent message carries context refs", () => {
    const msg = new AgentMessage("root", "评审意见", ["ref://r"])
    expect(msg.contextRefs).toEqual(["ref://r"])
  })

  it("agent spec holds runner and codec", () => {
    const runner = new EchoRunner()
    const codec = new JsonCodec()
    const spec = new AgentSpec(runner, codec)
    expect(spec.runner).toBeInstanceOf(EchoRunner)
    expect(spec.codec).toBeInstanceOf(JsonCodec)
  })

  it("error code values match spec", () => {
    expect(Object.values(ErrorCode)).toEqual([
      "NOT_FOUND",
      "PERMISSION_DENIED",
      "BUSY",
      "CLOSED",
      "LIMIT_REACHED",
      "BUDGET_EXHAUSTED",
      "INVALID_REQUEST",
      "CODEC_ERROR",
      "STORE_UNAVAILABLE",
      "INTERNAL",
    ])
  })

  it("error hierarchy matches spec", () => {
    expect(new AgentCommandError(ErrorCode.NOT_FOUND, "x")).toBeInstanceOf(AgentError)
    expect(new AgentBusyError()).toBeInstanceOf(AgentCommandError)
    expect(new AgentRunFailed()).toBeInstanceOf(AgentError)
    expect(new AgentRunInterrupted()).toBeInstanceOf(AgentError)
    expect(new AgentBusyError().code).toBe(ErrorCode.BUSY)
  })
})
