import { describe, expect, it } from "vitest"
import { AgentTypeRegistry } from "../../src/agent/registry.js"
import {
  AgentCommandError,
  AgentSpec,
  ErrorCode,
  JsonCodec,
  type AgentRunner,
  type EventSink,
} from "../../src/agent/types.js"

class EchoRunner implements AgentRunner {
  async run(request: unknown, _session: unknown, _emit: EventSink): Promise<unknown> {
    return { echo: request }
  }
}

class CountingRunner implements AgentRunner {
  calls: unknown[] = []
  async run(request: unknown, _session: unknown, _emit: EventSink): Promise<unknown> {
    this.calls.push(request)
    return { echo: request }
  }
}

function factory(): (aid: string, cfg: string | null) => AgentSpec {
  return (_aid, _cfg) => new AgentSpec(new EchoRunner(), new JsonCodec())
}

describe("AgentTypeRegistry", () => {
  it("register and require spec round trip", () => {
    const registry = new AgentTypeRegistry()
    registry.register("data", factory())
    expect(registry.requireSpec("data", "a1")).not.toBeNull()
    expect(registry.types).toEqual(["data"])
  })

  it("duplicate register rejected", () => {
    const registry = new AgentTypeRegistry()
    registry.register("data", factory())
    expect(() => registry.register("data", factory())).toThrow(/already registered/)
  })

  it("unregistered type raises not found", () => {
    const registry = new AgentTypeRegistry()
    let err: unknown
    try {
      registry.requireSpec("missing", "a1")
    } catch (e) {
      err = e
    }
    expect(err).toBeInstanceOf(AgentCommandError)
    expect((err as AgentCommandError).code).toBe(ErrorCode.NOT_FOUND)
  })

  it("types are sorted", () => {
    const registry = new AgentTypeRegistry()
    registry.register("plot", factory())
    registry.register("data", factory())
    expect(registry.types).toEqual(["data", "plot"])
  })

  it("factory creates independent binding per agent id", () => {
    const registry = new AgentTypeRegistry()
    registry.register(
      "echo",
      (_aid, _cfg) => new AgentSpec(new CountingRunner(), new JsonCodec())
    )
    const first = registry.requireSpec("echo", "a1").runner as CountingRunner
    const second = registry.requireSpec("echo", "a2").runner as CountingRunner
    expect(first).not.toBe(second)
    expect(first.calls).toEqual([])
    expect(second.calls).toEqual([])
  })
})
