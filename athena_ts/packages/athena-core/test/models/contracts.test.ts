import { describe, expect, it } from "vitest"
import { newId } from "../../src/id.js"
import { EventEnvelopeSchema } from "../../src/models/contracts.js"
import { AthenaThreadSchema, AthenaTurnSchema } from "../../src/models/thread-models.js"

describe("canonical contracts construct real domain records", () => {
  it("newId and EventEnvelope", () => {
    expect(newId("run").startsWith("run_")).toBe(true)
    const envelope = EventEnvelopeSchema.parse({
      kind: "experiment.started",
      source: "orchestrator",
      payload: { ref: "artifact://input" },
      state_version: 0,
    })
    expect(envelope.payload.ref).toBe("artifact://input")
  })
  it("thread and turn", () => {
    const thread = AthenaThreadSchema.parse({
      thread_id: "thread-1",
      session_id: "session-1",
      status: "running",
      context_ref: "artifact://context",
    })
    const turn = AthenaTurnSchema.parse({
      turn_id: "turn-1",
      thread_id: thread.thread_id,
      request_ref: "artifact://request",
      status: "running",
    })
    expect(turn.thread_id).toBe(thread.thread_id)
  })
})
