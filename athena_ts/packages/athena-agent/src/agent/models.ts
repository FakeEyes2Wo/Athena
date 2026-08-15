/**
 * Agent runtime 与控制组件共享的数据模型（移植 ``core/agent/models.py``）。
 */

import type { AthenaThread, AthenaTurn } from "@athena/core"
import type { CancellationToken } from "../cancel.js"
import type { ContextManager } from "../memory/context-manager.js"
import type { ToolRegistry } from "../tool.js"
import type { AskUser, EmitEvent } from "../tool-types.js"
import type { AgentMessage } from "./types.js"

/** Agent 运行配置 — 模型、系统提示、工具集和采样参数。 */
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
export class AgentOutcome {
  constructor(public resultRef: string, public nextContextRef: string = "") {}
}

/** 采样步进结果 — kind 驱动循环分派。 */
export class StepOutcome {
  constructor(
    public kind: "done" | "continue" | "error",
    public text: string = ""
  ) {}
}

/** LLM 输出的工具调用 — 包含调用 ID、名称和参数。 */
export class ToolCall {
  constructor(
    public callId: string,
    public name: string,
    public args: Record<string, unknown>
  ) {}
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

/** 每 Turn 上下文。memory 由 ThreadRuntime 注入，Agent 不自行创建。 */
export class AgentContext {
  constructor(
    public thread: AthenaThread,
    public turn: AthenaTurn,
    public emit: EmitEvent,
    public tools: ToolRegistry,
    public cancel: CancellationToken,
    public memory: ContextManager | null = null,
    public inputText: string | null = null,
    public messages: AgentMessage[] = [],
    public askUser: AskUser | null = null
  ) {}
}
