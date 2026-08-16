import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { Context } from "cordis"
import { HypothesisSchema, LocalGitWorkspace, ResearchTree } from "@athena/core"
import { DataScriptRunner, FixedFlowSupervisor, ResearchState, Scheduler, TrustedEvaluator } from "@athena/research"
import { researchPlugin, researchStatus } from "../src/index.js"

let tmp: string
afterEach(() => {
  if (tmp) rmSync(tmp, { recursive: true, force: true })
})

describe("researchPlugin", () => {
  it("registers the Athena research services on the context", async () => {
    tmp = mkdtempSync(join(tmpdir(), "athena-dsh-"))
    const ctx = new Context()
    await ctx.plugin(researchPlugin({ projectRoot: tmp }))

    expect(ctx.researchTree).toBeInstanceOf(ResearchTree)
    expect(ctx.researchState).toBeInstanceOf(ResearchState)
    expect(ctx.researchScheduler).toBeInstanceOf(Scheduler)
    expect(ctx.researchEvaluator).toBeInstanceOf(TrustedEvaluator)
    expect(ctx.researchScriptRunner).toBeInstanceOf(DataScriptRunner)
    expect(ctx.researchStore).toBeTruthy()
    expect(ctx.researchGit).toBeInstanceOf(LocalGitWorkspace)
    expect(ctx.researchSupervisor).toBeInstanceOf(FixedFlowSupervisor)

    // 只读状态快照可用。
    expect(researchStatus(ctx).phase).toBe("PREPARE")

    // 研究态可写、树可加假设——Service 是活的。
    ctx.researchTree.addHypothesis(
      HypothesisSchema.parse({
        statement: "claim",
        intervention: "change",
        expected_effect: "improve",
      })
    )
    expect(ctx.researchTree.pendingHypotheses().length).toBe(1)
  })

  it("registers research_status/research_tree/research_run when the DSH tool service exists", async () => {
    tmp = mkdtempSync(join(tmpdir(), "athena-dsh-tools-"))
    const registered: string[] = []
    const ctx = new Context()
    ctx.provide("tools", {
      register(def: { name: string }) {
        registered.push(def.name)
        return () => {}
      },
    })
    await ctx.plugin(researchPlugin({ projectRoot: tmp }))

    expect(registered).toEqual(["research_status", "research_tree", "research_run"])
    expect(researchStatus(ctx).phase).toBe("PREPARE")
  })
})
