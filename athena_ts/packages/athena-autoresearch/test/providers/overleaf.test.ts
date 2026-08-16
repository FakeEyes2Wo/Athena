import { describe, expect, it } from "vitest"
import { OverleafCompiler } from "../../src/index.js"
import { makeContext } from "../_support.js"

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

describe("OverleafCompiler", () => {
  it("creates project, compiles, and returns console log", async () => {
    const calls: string[] = []
    const compiler = new OverleafCompiler({
      apiToken: "token",
      apiBaseUrl: "https://api.example.invalid",
      fetchImpl: (async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        calls.push(url)
        if (url.endsWith("/projects")) return jsonResponse({ id: "proj-1" })
        if (url.includes("/compile")) return jsonResponse({ compile_id: "c-1" })
        if (url.includes("/output/log")) return new Response("compiled ok", { status: 200 })
        return jsonResponse({}, 200)
      }) as typeof fetch,
    })
    const result = await compiler.compile(makeContext(), "paper/ar_test")
    expect(result.ok).toBe(true)
    expect(result.compiler).toBe("overleaf")
    expect(calls.some((url) => url.includes("/projects"))).toBe(true)
    expect(calls.some((url) => url.includes("/compile"))).toBe(true)
    expect(calls.some((url) => url.includes("/output/log"))).toBe(true)
  })
})
