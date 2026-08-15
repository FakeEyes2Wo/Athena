/**
 * LLM 配置：从环境变量加载 DeepSeek API 凭据并构造 OpenAI 兼容 client（移植 ``core/agent/settings.py``）。
 */

export const DEFAULT_BASE_URL = "https://api.deepseek.com"
export const DEFAULT_MODEL = "deepseek-v4-flash"
export const DEFAULT_PRO_MODEL = "deepseek-v4-pro"

export const ALLOWED_PROVIDERS = ["deepseek", "openai", "anthropic"] as const
export type ProviderKind = (typeof ALLOWED_PROVIDERS)[number]

/** OpenAI 兼容 chat.completions 客户端的最小结构（真实 SDK 集成推迟到 M2）。 */
export interface ChatClient {
  chat: {
    completions: {
      create(kwargs: Record<string, unknown>): Promise<AsyncIterable<unknown>>
    }
  }
}

function resolve(key: string, def?: string): string | undefined {
  const value = process.env[key]
  return value ? value : def
}

/** 读取 DEEPSEEK_API_KEY，回退 OPENAI_API_KEY。 */
export function apiKey(): string | null {
  return resolve("DEEPSEEK_API_KEY", resolve("OPENAI_API_KEY")) ?? null
}

/** DeepSeek OpenAI 兼容端点。 */
export function baseUrl(): string {
  return resolve("BASE_URL", DEFAULT_BASE_URL) ?? DEFAULT_BASE_URL
}

/** 默认 deepseek-v4-flash（最便宜档位），可经 MODEL_NAME 覆盖。 */
export function modelName(): string {
  return resolve("MODEL_NAME", DEFAULT_MODEL) ?? DEFAULT_MODEL
}

/** Pro 档位模型，可经 MODEL_PRO 覆盖。 */
export function proModelName(): string {
  return resolve("MODEL_PRO", DEFAULT_PRO_MODEL) ?? DEFAULT_PRO_MODEL
}

/** LLM 后端类型：仅显式 LLM_PROVIDER 环境变量；非法值抛错。 */
export function providerKind(): ProviderKind {
  const kind = resolve("LLM_PROVIDER", "deepseek")
  if (!ALLOWED_PROVIDERS.includes(kind as ProviderKind)) {
    throw new Error(
      `unsupported LLM_PROVIDER=${JSON.stringify(kind)}; expected one of ${ALLOWED_PROVIDERS.join(", ")}`
    )
  }
  return kind as ProviderKind
}

/** 构造 OpenAI 兼容 client；无 API key 直接报错（真实 SDK 集成推迟到 M2）。 */
export function getClient(): ChatClient {
  const key = apiKey()
  if (!key) {
    throw new Error(
      "Missing LLM API key: set DEEPSEEK_API_KEY or OPENAI_API_KEY in .env"
    )
  }
  return {
    chat: {
      completions: {
        async create(): Promise<AsyncIterable<unknown>> {
          throw new Error("real OpenAI client wiring deferred to M2")
        },
      },
    },
  }
}
