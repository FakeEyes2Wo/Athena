import { describe, expect, it } from "vitest"
import { isTransientError, retryAsync, TimeoutError } from "../../src/services/retry.js"

describe("isTransientError classifies", () => {
  it("whitelist transient vs business/auth", () => {
    expect(isTransientError(new Error("timed out"))).toBe(true) // message token
    expect(isTransientError(new TimeoutError("timed out"))).toBe(true)
    expect(isTransientError("APITimeoutError: Request timed out")).toBe(true)
    expect(isTransientError("openai.RateLimitError: Error code: 429")).toBe(true)
    expect(isTransientError("InternalServerError: Error code: 503")).toBe(true)
    expect(isTransientError("database is locked")).toBe(true)
    expect(isTransientError("connection reset")).toBe(true)
    expect(isTransientError(new TypeError("contract violation"))).toBe(false)
    expect(isTransientError("AuthError: invalid api key")).toBe(false)
  })
})

describe("retryAsync", () => {
  it("retries transient then succeeds", async () => {
    let calls = 0
    const flaky = async () => { calls += 1; if (calls < 3) throw new TimeoutError("timed out"); return "ok" }
    const result = await retryAsync(flaky, { attempts: 4, baseDelay: 0 })
    expect(result).toBe("ok")
    expect(calls).toBe(3)
  })
  it("raises on non-transient", async () => {
    let calls = 0
    const bad = async () => { calls += 1; throw new TypeError("contract violation") }
    await expect(retryAsync(bad, { attempts: 4, baseDelay: 0 })).rejects.toThrow(/contract violation/)
    expect(calls).toBe(1)
  })
  it("gives up after exhaustion", async () => {
    const always = async () => { throw new TimeoutError("timed out") }
    await expect(retryAsync(always, { attempts: 2, baseDelay: 0 })).rejects.toThrow()
  })
})
