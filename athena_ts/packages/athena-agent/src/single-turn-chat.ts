/**
 * One-off Agent interaction with optional caller-owned conversation memory（移植 utils/single_turn_chat.py）。
 */

import { randomBytes } from "node:crypto"

import { AthenaThreadSchema, AthenaTurnSchema } from "@athena/core"

import { CancelledError } from "./agent/types.js"
import { AgentContext } from "./agent/models.js"
import { createAgent } from "./agent/runtime.js"
import type { ChatClient } from "./agent/settings.js"
import { ToolRegistry } from "./tool.js"
import type { EmitEvent } from "./tool-types.js"
import { ContextManager } from "./memory/context-manager.js"
import type { ModelMessage } from "./messages.js"

async function noopEmit(
  _kind: string,
  _ref: string,
  _data?: Record<string, unknown> | null
): Promise<void> {
  void 0
}

function validate(
  prompt: string,
  model: string,
  maxTurns: number,
  maxTokens: number,
  temperature: number
): void {
  if (typeof prompt !== "string" || !prompt.trim()) {
    throw new Error("prompt must be a non-empty string")
  }
  if (typeof model !== "string" || !model.trim()) {
    throw new Error("model must be a non-empty string")
  }
  if (typeof maxTurns !== "number" || !Number.isInteger(maxTurns) || maxTurns <= 0) {
    throw new Error("max_turns must be a positive integer")
  }
  if (typeof maxTokens !== "number" || !Number.isInteger(maxTokens) || maxTokens <= 0) {
    throw new Error("max_tokens must be a positive integer")
  }
  if (typeof temperature !== "number" || !(temperature >= 0 && temperature <= 2)) {
    throw new Error("temperature must be between 0 and 2")
  }
}

function finalText(messages: ModelMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i]!
    if (message.kind !== "response") continue
    if (message.parts.some((p) => p.part_kind === "tool-call")) continue
    const text = message.parts
      .filter((p) => p.part_kind === "text" && "content" in p && typeof p.content === "string")
      .map((p) => ("content" in p ? p.content : ""))
      .join("")
    if (text.trim()) return text
  }
  throw new Error("single-turn chat produced no final assistant text")
}

export interface SingleTurnChatOptions {
  model: string
  memory?: ContextManager | null
  tools?: ToolRegistry | null
  systemPrompt?: string | null
  client?: ChatClient | null
  maxTurns?: number
  maxTokens?: number
  temperature?: number
  emit?: EmitEvent | null
  cancel?: AbortSignal | null
}

export async function singleTurnChat(prompt: string, opts: SingleTurnChatOptions): Promise<string> {
  const {
    model,
    memory = null,
    tools = null,
    systemPrompt = null,
    client = null,
    maxTurns = 200,
    maxTokens = 4096,
    temperature = 0.1,
    emit = null,
    cancel = null,
  } = opts

  validate(prompt, model, maxTurns, maxTokens, temperature)
  const activeCancel = cancel ?? new AbortController().signal
  if (activeCancel.aborted) throw new CancelledError()

  const activeMemory = memory ?? new ContextManager()
  const activeTools = tools ?? new ToolRegistry()
  const startIndex = activeMemory.snapshot()[0]
  const identity = randomBytes(16).toString("hex")
  const threadId = `thread:single-turn:${identity}`
  const turnId = `turn:single-turn:${identity}`

  const thread = AthenaThreadSchema.parse({
    thread_id: threadId,
    session_id: `session:single-turn:${identity}`,
    status: "running",
    context_ref: `context:single-turn:${identity}`,
  })
  const turn = AthenaTurnSchema.parse({
    turn_id: turnId,
    thread_id: threadId,
    request_ref: prompt,
    status: "running",
  })

  const agent = createAgent(model, activeTools, systemPrompt ?? "", {
    client,
    maxTurns,
    maxTokens,
    temperature,
    name: "single-turn-chat",
  })
  const context = new AgentContext(
    thread,
    turn,
    emit ?? noopEmit,
    activeTools,
    activeCancel,
    activeMemory,
    prompt
  )

  await agent.run(context)
  return finalText(activeMemory.items.slice(startIndex))
}
