/**
 * 工具类型、常量和轻量级数据类（移植 ``core/tool_types.py``，零逻辑纯数据容器）。
 */


/** 事件发射器: ``(kind: string, artifact_ref: string, data: dict | null) -> None``。 */
export type EmitEvent = (
  kind: string,
  artifactRef: string,
  data?: Record<string, unknown> | null
) => Promise<void> | void

/** 用户输入请求回调: ``(prompt) -> 回答文本``；``null`` 表示取消/超时。 */
export type AskUser = (prompt: string) => Promise<string | null> | string | null

export const TOOL_BEGIN = "tool/begin"
export const TOOL_END = "tool/end"
export const TOOL_ERROR = "tool/error"

/** 工具返回/日志文本写入对话历史的截断上限；保留头尾便于调试。 */
const MAX_RESULT_CHARS = 50_000
const TRUNCATED_MARK = "\n...[TRUNCATED]...\n"

/** 超过 ``limit`` 时保留头尾各一半，中间用截断标记连接。 */
export function truncateText(text: string, limit: number = MAX_RESULT_CHARS): string {
  if (text.length <= limit) return text
  const half = Math.floor((limit - TRUNCATED_MARK.length) / 2)
  return text.slice(0, half) + TRUNCATED_MARK + text.slice(text.length - half)
}

/** 工具描述 — 最小化字段（等价 ToolSpec dataclass）。 */
export class ToolSpec {
  constructor(
    public readonly name: string,
    public readonly description: string,
    public readonly inputSchema: Record<string, unknown>,
    public readonly concurrencySafe: boolean = true
  ) {}

  /** 转为 OpenAI function tool schema dict。 */
  toOpenaiTool(): Record<string, unknown> {
    return {
      type: "function",
      function: {
        name: this.name,
        description: this.description,
        parameters: this.inputSchema,
      },
    }
  }
}

/** 标准化的工具输出（等价 ToolResult dataclass）。 */
export class ToolResult {
  constructor(
    public readonly data: unknown,
    public readonly success: boolean = true,
    public readonly error: string | null = null,
    public readonly artifacts: string[] = []
  ) {}
}

/** 每次调用的上下文 — 每次工具调用时重新创建（等价 ToolContext dataclass）。 */
export class ToolContext {
  constructor(
    public readonly toolName: string,
    public readonly callId: string,
    public readonly emit: EmitEvent,
    public readonly cancel: AbortSignal,
    public readonly askUser: AskUser | null = null
  ) {}
}
