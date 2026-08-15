/**
 * PydanticAI ``ModelMessage`` 消息模型的最小 zod 移植。
 *
 * Python 侧 ``pydantic_ai.messages`` 的 ``ModelRequest``/``ModelResponse`` 及
 * ``SystemPromptPart``/``UserPromptPart``/``TextPart``/``ToolCallPart``/
 * ``ToolReturnPart`` 在此以 discriminated union 复刻，供 memory（context-manager/
 * compaction/rollout）与 agent（provider/runtime）共享。JSON 往返采用本包自定义
 * 格式（``kind`` 区分 request/response，``part_kind`` 区分 part），与 Python
 * 侧 ModelMessagesTypeAdapter 语义对齐（结构往返，非字节级一致）。
 */

import { z } from "zod"

/** 系统提示词 part。pydantic: SystemPromptPart(part_kind="system-prompt")。 */
export const SystemPromptPartSchema = z.object({
  part_kind: z.literal("system-prompt"),
  content: z.string(),
})
export type SystemPromptPart = z.infer<typeof SystemPromptPartSchema>

/** 用户提示词 part。pydantic: UserPromptPart(part_kind="user-prompt")。 */
export const UserPromptPartSchema = z.object({
  part_kind: z.literal("user-prompt"),
  content: z.string(),
})
export type UserPromptPart = z.infer<typeof UserPromptPartSchema>

/** 纯文本 part（可出现在 request 与 response）。pydantic: TextPart。 */
export const TextPartSchema = z.object({
  part_kind: z.literal("text"),
  content: z.string(),
})
export type TextPart = z.infer<typeof TextPartSchema>

/** 工具调用 part。pydantic: ToolCallPart(tool_name, tool_call_id, args)。 */
export const ToolCallPartSchema = z.object({
  part_kind: z.literal("tool-call"),
  tool_name: z.string(),
  tool_call_id: z.string().nullable().default(null),
  args: z.unknown(),
})
export type ToolCallPart = z.infer<typeof ToolCallPartSchema>

/** 工具返回 part。pydantic: ToolReturnPart(tool_name, content, tool_call_id)。 */
export const ToolReturnPartSchema = z.object({
  part_kind: z.literal("tool-return"),
  tool_name: z.string(),
  content: z.string(),
  tool_call_id: z.string().nullable().default(null),
})
export type ToolReturnPart = z.infer<typeof ToolReturnPartSchema>

/** ModelRequest 允许的 part 集合（system/user/text/tool-return）。 */
export const ModelRequestPartSchema = z.discriminatedUnion("part_kind", [
  SystemPromptPartSchema,
  UserPromptPartSchema,
  TextPartSchema,
  ToolReturnPartSchema,
])
export type ModelRequestPart = z.infer<typeof ModelRequestPartSchema>

/** ModelResponse 允许的 part 集合（text/tool-call）。 */
export const ModelResponsePartSchema = z.discriminatedUnion("part_kind", [
  TextPartSchema,
  ToolCallPartSchema,
])
export type ModelResponsePart = z.infer<typeof ModelResponsePartSchema>

/** 请求消息。pydantic: ModelRequest(kind="request")。 */
export const ModelRequestSchema = z.object({
  kind: z.literal("request"),
  parts: z.array(ModelRequestPartSchema),
})
export type ModelRequest = z.infer<typeof ModelRequestSchema>

/** 响应消息。pydantic: ModelResponse(kind="response")。 */
export const ModelResponseSchema = z.object({
  kind: z.literal("response"),
  parts: z.array(ModelResponsePartSchema),
})
export type ModelResponse = z.infer<typeof ModelResponseSchema>

/** 消息联合类型。pydantic: ModelMessage = ModelRequest | ModelResponse。 */
export const ModelMessageSchema = z.discriminatedUnion("kind", [
  ModelRequestSchema,
  ModelResponseSchema,
])
export type ModelMessage = z.infer<typeof ModelMessageSchema>

// —— 便捷构造器（等价 pydantic_ai 的 part/message 工厂用法）——

export function systemPrompt(content: string): SystemPromptPart {
  return { part_kind: "system-prompt", content }
}

export function userPrompt(content: string): UserPromptPart {
  return { part_kind: "user-prompt", content }
}

export function textPart(content: string): TextPart {
  return { part_kind: "text", content }
}

export function toolCallPart(
  tool_name: string,
  args: unknown,
  tool_call_id: string | null = null
): ToolCallPart {
  return { part_kind: "tool-call", tool_name, tool_call_id, args }
}

export function toolReturnPart(
  tool_name: string,
  content: string,
  tool_call_id: string | null = null
): ToolReturnPart {
  return { part_kind: "tool-return", tool_name, content, tool_call_id }
}

export function modelRequest(parts: ModelRequestPart[]): ModelRequest {
  return { kind: "request", parts }
}

export function modelResponse(parts: ModelResponsePart[]): ModelResponse {
  return { kind: "response", parts }
}

/**
 * 等价 pydantic_ai ``ModelMessagesTypeAdapter``：列表 ↔ JSON 值往返。
 * ``dump`` 输出 JSON 安全值（深拷贝 + 去 undefined）；``parse`` 校验并重建。
 */
export const ModelMessagesTypeAdapter = {
  dump(messages: ModelMessage[]): unknown {
    return JSON.parse(JSON.stringify(messages))
  },
  parse(payload: unknown): ModelMessage[] {
    return z.array(ModelMessageSchema).parse(payload)
  },
}
