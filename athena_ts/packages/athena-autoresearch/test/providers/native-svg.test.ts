import { describe, expect, it } from "vitest"
import { NativeSvgProvider } from "../../src/index.js"
import { makeContext } from "../_support.js"

describe("NativeSvgProvider", () => {
  it("self-test passes", async () => {
    const provider = new NativeSvgProvider()
    const result = await provider.selfTest(makeContext())
    expect(result.ok).toBe(true)
  })

  it("generates a valid-looking SVG artifact", async () => {
    const ctx = makeContext()
    const provider = new NativeSvgProvider()
    const result = await provider.generate(ctx, {
      runId: ctx.runId,
      name: "architecture",
      kind: "architecture",
      title: "Test Architecture",
      context: { sotaPath: ["baseline", "method"], evidence: {}, draftSectionRefs: [] },
      constraints: {
        format: "svg",
        maxWidthPx: 800,
        maxHeightPx: 600,
        palette: ["#ffffff", "#333333", "#111111"],
        noExternalAssets: true,
      },
    })
    expect(result.selfCheck.syntaxOk).toBe(true)
    expect(result.svgPath).toBe("figures/architecture.svg")
    const svg = await ctx.artifacts.getText(result.svgRef)
    expect(svg).toContain("<svg")
    expect(svg).toContain("viewBox")
    expect(svg).toContain("Test Architecture")
  })
})
