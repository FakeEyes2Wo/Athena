import { z } from "zod"
import { ArtifactRef } from "./contracts.js"

/** 对话线程——对应一个 Agent 会话。 */
export const AthenaThreadSchema = z.object({
  thread_id: z.string(),
  session_id: z.string(),
  status: z.string(),
  context_ref: ArtifactRef,
})
export type AthenaThread = z.infer<typeof AthenaThreadSchema>

/** 单轮对话——请求引用与可选执行结果引用。 */
export const AthenaTurnSchema = z.object({
  turn_id: z.string(),
  thread_id: z.string(),
  request_ref: ArtifactRef,
  status: z.string(),
  result_ref: ArtifactRef.nullable().default(null),
})
export type AthenaTurn = z.infer<typeof AthenaTurnSchema>
