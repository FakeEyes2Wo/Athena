import { afterEach, describe, expect, it } from "vitest"
import { apiKey, baseUrl, getClient, modelName, proModelName } from "../../src/agent/settings.js"

const ENV_KEYS = [
  "DEEPSEEK_API_KEY",
  "OPENAI_API_KEY",
  "BASE_URL",
  "MODEL_NAME",
  "MODEL_PRO",
  "LLM_PROVIDER",
]

let restore: (() => void) | null = null

function clearEnv() {
  const saved: Record<string, string | undefined> = {}
  for (const k of ENV_KEYS) {
    saved[k] = process.env[k]
    delete process.env[k]
  }
  return () => {
    for (const k of ENV_KEYS) {
      if (saved[k] === undefined) delete process.env[k]
      else process.env[k] = saved[k]
    }
  }
}

afterEach(() => {
  restore?.()
  restore = null
})

describe("settings", () => {
  it("defaults when env unset", () => {
    restore = clearEnv()
    expect(baseUrl()).toBe("https://api.deepseek.com")
    expect(modelName()).toBe("deepseek-v4-flash")
    expect(proModelName()).toBe("deepseek-v4-pro")
    expect(apiKey()).toBeNull()
  })

  it("reads env", () => {
    restore = clearEnv()
    process.env.DEEPSEEK_API_KEY = "sk-test"
    process.env.BASE_URL = "https://api.deepseek.com"
    process.env.MODEL_NAME = "deepseek-chat"
    process.env.MODEL_PRO = "deepseek-reasoner"
    expect(apiKey()).toBe("sk-test")
    expect(modelName()).toBe("deepseek-chat")
    expect(proModelName()).toBe("deepseek-reasoner")
  })

  it("get_client raises without key", () => {
    restore = clearEnv()
    expect(() => getClient()).toThrow(/API key/)
  })
})
