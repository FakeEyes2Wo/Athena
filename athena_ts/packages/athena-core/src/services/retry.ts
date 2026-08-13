/** 对应 Python 内建 TimeoutError（瞬时错误类型白名单成员）；Node 无此全局。 */
export class TimeoutError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "TimeoutError"
  }
}

// 瞬时错误消息特征（大小写不敏感），逐字照抄 _TRANSIENT_TOKENS。
const TRANSIENT_TOKENS = [
  "timeout",
  "timed out",
  "connection reset",
  "connection refused",
  "connection error",
  "connection dropped",
  "remote protocol",
  "broken pipe",
  "rate limit",
  "too many requests",
  "error code: 429",
  "error code: 5",
  "status 429",
  "status 5",
  "5xx",
  "internal server error",
  "service unavailable",
  "bad gateway",
  "api connection error",
  "api timeout",
  "apiconnectionerror",
  "apitimeouterror",
  "ratelimiterror",
  "database is locked",
  "database is busy",
  "operationalerror",
] as const

/** Node 网络层瞬时错误 code 白名单（等价 _TRANSIENT_EXC_TYPES 的类型判断）。 */
const TRANSIENT_CODES = new Set([
  "ECONNRESET", "ECONNREFUSED", "ECONNABORTED", "ETIMEDOUT", "EPIPE", "ENOTFOUND", "EAI_AGAIN",
])

/** 判定异常（或异常文本）是否属于可自动重试的瞬时基础设施错误。 */
export function isTransientError(excOrMsg: unknown): boolean {
  if (excOrMsg instanceof Error) {
    const err = excOrMsg as Error & { code?: unknown; status?: unknown; status_code?: unknown }
    if (err instanceof TimeoutError) return true
    if (typeof err.code === "string" && TRANSIENT_CODES.has(err.code)) return true
    const status = typeof err.status === "number" ? err.status : typeof err.status_code === "number" ? err.status_code : null
    if (status !== null && (status === 429 || status >= 500)) return true
    const text = `${err.name}: ${err.message}`.toLowerCase()
    return TRANSIENT_TOKENS.some((t) => text.includes(t))
  }
  const text = String(excOrMsg).toLowerCase()
  return TRANSIENT_TOKENS.some((t) => text.includes(t))
}

export interface RetryOptions {
  attempts?: number
  baseDelay?: number
  maxDelay?: number
  classify?: (error: Error) => boolean
}

/** 指数退避 + jitter 重试；仅对瞬时错误重试，最后一次失败原样抛出。 */
export async function retryAsync<T>(
  fn: () => Promise<T>,
  { attempts = 4, baseDelay = 1.0, maxDelay = 8.0, classify = isTransientError }: RetryOptions = {},
): Promise<T> {
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      return await fn()
    } catch (err) {
      const error = err as Error
      if (attempt === attempts - 1 || !classify(error)) throw error
      const delay = Math.min(maxDelay, baseDelay * 2 ** attempt) * (0.5 + Math.random())
      await new Promise((resolve) => setTimeout(resolve, delay))
    }
  }
  throw new Error("unreachable")
}
