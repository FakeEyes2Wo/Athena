import { z } from "zod"

/** 至少含一个非空白字符的文本。pydantic: Annotated[str, StringConstraints(pattern=r"\S")] */
export const NonBlankText = z.string().regex(/\S/)
export type NonBlankText = z.infer<typeof NonBlankText>

export const ArtifactRef = NonBlankText
export type ArtifactRef = z.infer<typeof ArtifactRef>

export const CommitHash = NonBlankText
export type CommitHash = z.infer<typeof CommitHash>

/** 事件信封——事件类型、来源、载荷与状态版本号。 */
export const EventEnvelopeSchema = z.object({
  kind: z.string(),
  source: z.string(),
  payload: z.record(z.string(), z.unknown()),
  state_version: z.number().int().min(0),
})
export type EventEnvelope = z.infer<typeof EventEnvelopeSchema>

/** 错误记录——严重级别、错误码、消息、重试次数。 */
export const ErrorRecordSchema = z.object({
  severity: z.enum(["retry", "degrade", "fatal"]),
  code: z.string(),
  message: z.string(),
  retry_count: z.number().int().min(0),
})
export type ErrorRecord = z.infer<typeof ErrorRecordSchema>

/** 论文工具与工作流共享的最小异步 artifact 契约（等价 ArtifactStore Protocol）。 */
export interface ArtifactStore {
  putBytes(data: Uint8Array): Promise<ArtifactRef>
  getBytes(ref: ArtifactRef): Promise<Uint8Array>
  putText(text: string): Promise<ArtifactRef>
  getText(ref: ArtifactRef): Promise<string>
}
