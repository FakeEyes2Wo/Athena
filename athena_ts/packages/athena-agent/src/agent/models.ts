/**
 * Agent runtime 与控制组件共享的数据模型（移植 ``core/agent/models.py``）。
 */

import type { AthenaThread, AthenaTurn } from "@athena/core"
import type { ContextManager } from "../memory/context-manager.js"
import type { ToolRegistry } from "../tool.js"
import type { AskUser, EmitEvent } from "../tool-types.js"

/** Agent 采样策略与名称。 */
export class AgentConfig {
  constructor(
    public maxTurns: number = 200,
    public maxTokens: number = 4096,
    public temperature: number = 0.1,
    public name: string = "code-agent",
    public toolChoice: "auto" | "required" = "auto"
  ) {}
}

/** Agent 运行结果 — 目标合同只含 ``resultRef``。 */
export interface AgentOutcome {
  resultRef: string
}

/** 采样步进结果 — kind 驱动循环分派。 */
export type StepOutcome =
  | { kind: "continue" }
  | { kind: "done" | "error"; text: string }

/** LLM 输出的工具调用 — 包含调用 ID、名称和参数。 */
export interface ToolCall {
  callId: string
  name: string
  args: Record<string, unknown>
}

/**
 * 结构化输出的 output_type 契约（等价 pydantic BaseModel）：
 * provider 用 ``name``/``modelJsonSchema`` 注入 schema，runtime 用 ``parseJson``/``toJson`` 校验。
 */
export interface StructuredOutputType {
  name: string
  modelJsonSchema(): unknown
  parseJson(text: string): unknown
  toJson(value: unknown): string
}

/** 每 Turn 上下文；调用方可复用 memory，缺省时由 Agent 创建。 */
export class AgentContext {
  constructor(
    public thread: AthenaThread,
    public turn: AthenaTurn,
    public emit: EmitEvent,
    public tools: ToolRegistry,
    public cancel: AbortSignal,
    public memory: ContextManager | null = null,
    public inputText: string | null = null,
    public askUser: AskUser | null = null
  ) {}
}
