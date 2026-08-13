import type { z } from "zod"

/** 领域错误基类——映射 Python ValueError/FileNotFoundError/OSError 语义的顶层封装。 */
export class AthenaError extends Error {
  constructor(message: string) {
    super(message)
    this.name = new.target.name
  }
}

export class AthenaValidationError extends AthenaError {}
export class AthenaNotFoundError extends AthenaError {}
export class AthenaIntegrityError extends AthenaError {}
export class AthenaClosedError extends AthenaError {}

/** 把 zod issue 路径渲染为点号串，使消息含字段名（对齐 pytest 的 match="字段名"）。 */
export function formatZodError(error: z.ZodError): string {
  const issue = error.issues[0]
  const path = issue?.path?.join(".") ?? ""
  return `${path ? path + ": " : ""}${issue?.message ?? "validation failed"}`
}

/** 解析 schema；失败抛 AthenaValidationError，消息含字段路径。 */
export function parseOrThrow<T>(schema: z.ZodType<T>, data: unknown): T {
  const result = schema.safeParse(data)
  if (result.success) return result.data
  throw new AthenaValidationError(formatZodError(result.error))
}

/** model_dump(mode="json") 等价：深拷贝 + 去 undefined + JSON 安全化。 */
export function toJSON<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}
