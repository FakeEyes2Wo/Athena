import { existsSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { PaperComposer, PackagingService } from "../../src/index.js"
import { makeContext } from "../_support.js"

describe("PaperComposer", () => {
  it("composes a paper draft and sections", async () => {
    const ctx = makeContext()
    const composer = new PaperComposer()
    const paper = await composer.compose(ctx)
    expect(existsSync(paper.draftPath)).toBe(true)
    expect(paper.sections).toContain("method")
    expect(await ctx.artifacts.getText(paper.draftRef)).toContain("Untitled AutoResearch")
  })

  it("packages experiment evidence", async () => {
    const ctx = makeContext()
    const packaging = new PackagingService()
    const result = await packaging.package(ctx)
    expect(existsSync(`${result.root}/experiment_evidence.json`)).toBe(true)
  })
})
