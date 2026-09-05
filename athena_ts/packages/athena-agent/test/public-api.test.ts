import { describe, expect, it } from "vitest"
import * as api from "../src/index.js"
import { Agent, BaseAgent, agentRunner, createAgent, createCodeAgent } from "../src/agent/runtime.js"
import {
  AgentConfig,
  AgentContext,
  AgentOutcome,
  StepOutcome,
  ToolCall,
} from "../src/agent/models.js"
import {
  DeepSeekProvider,
  OpenAIProvider,
  ResponsesProvider,
  StreamEvent,
  createProvider,
} from "../src/agent/provider.js"

describe("public surface", () => {
  it("does not export the removed memory facade", () => {
    expect(api).not.toHaveProperty("MemoryView")
  })

  it("exports canonical agent symbols", () => {
    const names = [
      "BaseTool",
      "ToolRegistry",
      "ToolResult",
      "ToolSpec",
      "ToolContext",
      "Agent",
      "AgentConfig",
      "AgentContext",
      "AgentOutcome",
      "BaseAgent",
      "ResponsesProvider",
      "StepOutcome",
      "StreamEvent",
      "ToolCall",
      "agentRunner",
      "createAgent",
      "createCodeAgent",
      "createProvider",
      "OpenAIProvider",
      "DeepSeekProvider",
      "RequestUserInputTool",
      "ContextManager",
      "Compactor",
      "RolloutRecorder",
    ]
    for (const name of names) {
      expect((api as Record<string, unknown>)[name]).toBeTruthy()
    }
  })

  it("exports point to canonical owners", () => {
    expect(api.Agent).toBe(Agent)
    expect(api.AgentConfig).toBe(AgentConfig)
    expect(api.AgentContext).toBe(AgentContext)
    expect(api.AgentOutcome).toBe(AgentOutcome)
    expect(api.BaseAgent).toBe(BaseAgent)
    expect(api.ResponsesProvider).toBe(ResponsesProvider)
    expect(api.StepOutcome).toBe(StepOutcome)
    expect(api.StreamEvent).toBe(StreamEvent)
    expect(api.ToolCall).toBe(ToolCall)
    expect(api.agentRunner).toBe(agentRunner)
    expect(api.createAgent).toBe(createAgent)
    expect(api.createCodeAgent).toBe(createCodeAgent)
    expect(api.createProvider).toBe(createProvider)
    expect(api.OpenAIProvider).toBe(OpenAIProvider)
    expect(api.DeepSeekProvider).toBe(DeepSeekProvider)
  })
})
