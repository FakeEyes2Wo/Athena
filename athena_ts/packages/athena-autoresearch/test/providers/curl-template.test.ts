import { describe, expect, it } from "vitest"
import { CurlTemplateProvider } from "../../src/index.js"
import { makeTmpDir } from "../_support.js"

describe("CurlTemplateProvider", () => {
  it("fetches template via injected fetchImpl", async () => {
    const calls: Array<[string, string]> = []
    const provider = new CurlTemplateProvider({
      cacheRoot: `${makeTmpDir()}/templates`,
      registry: {
        iclr2026: { url: "https://example.invalid/iclr.zip", mainFile: "main.tex", rootDir: "iclr" },
      },
      fetchImpl: async (url, dest) => {
        calls.push([url, dest])
        const { writeFileSync } = await import("node:fs")
        writeFileSync(dest, "fake-zip")
      },
    })
    const dir = await provider.fetch("iclr2026")
    expect(dir.source).toBe("network")
    expect(dir.mainFile).toBe("main.tex")
    expect(calls.length).toBe(1)
  })

  it("falls back to cache when fetch fails", async () => {
    const cacheRoot = `${makeTmpDir()}/templates`
    const provider = new CurlTemplateProvider({
      cacheRoot,
      registry: {
        iclr2026: { url: "https://example.invalid/iclr.zip", mainFile: "main.tex", rootDir: "iclr" },
      },
      fetchImpl: async () => {
        throw new Error("network down")
      },
    })
    await expect(provider.fetch("iclr2026")).rejects.toThrow(/template fetch failed/)
  })
})
