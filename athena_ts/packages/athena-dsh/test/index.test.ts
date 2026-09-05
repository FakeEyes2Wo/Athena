import { mkdtempSync, readFileSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, relative } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import { Context } from "cordis"
import { HypothesisSchema, LocalGitWorkspace, ResearchTree } from "@athena/core"
import { DataScriptRunner, FixedFlowSupervisor, parseResearchState, Scheduler, TrustedEvaluator } from "@athena/research"
import { dshWorkers, researchPlugin, researchStatus } from "../src/index.js"

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
    expect(Object.getPrototypeOf(ctx.researchState)).toBe(Object.prototype)
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

  it("registers the core Athena research tool set when the DSH tool service exists", async () => {
    tmp = mkdtempSync(join(tmpdir(), "athena-dsh-tools-"))
    const registered: Array<{ name: string; execute: (args: unknown, exec: unknown) => Promise<unknown> }> = []
    const ctx = new Context()
    ctx.provide("tools", {
      register(def: { name: string; execute: (args: unknown, exec: unknown) => Promise<unknown> }) {
        registered.push(def)
        return () => {}
      },
    })
    await ctx.plugin(researchPlugin({ projectRoot: tmp }))

    expect(registered.map((def) => def.name)).toEqual([
      "research_status",
      "research_tree",
      "research_hypotheses",
      "research_plans",
      "research_propose_hypothesis",
      "research_select_hypothesis",
      "research_configure_search",
      "research_set_manual_mode",
      "research_update_plan_budget",
      "research_set_phase_decision",
      "research_guidance",
      "research_task_understanding",
      "research_pause",
      "research_resume",
      "research_stop",
      "research_validate",
      "research_report",
      "research_run",
    ])
    expect(researchStatus(ctx).phase).toBe("PREPARE")
  })

  it("read tools execute against the live research services", async () => {
    tmp = mkdtempSync(join(tmpdir(), "athena-dsh-exec-"))
    const registered: Array<{ name: string; execute: (args: unknown, exec: unknown) => Promise<unknown> }> = []
    const ctx = new Context()
    ctx.provide("tools", {
      register(def: { name: string; execute: (args: unknown, exec: unknown) => Promise<unknown> }) {
        registered.push(def)
        return () => {}
      },
    })
    await ctx.plugin(researchPlugin({ projectRoot: tmp, searchLimit: 3 }))

    const find = (name: string) => registered.find((def) => def.name === name)!

    const status = await find("research_status").execute({}, {})
    expect(status).toMatchObject({ phase: "PREPARE", status: "RUNNING", search_limit: 3 })

    const report = await find("research_report").execute({}, {})
    expect((report as { report: string }).report).toContain("# Athena 研究报告")

    const paused = await find("research_pause").execute({}, {})
    expect(paused).toEqual({ result: "WAITING" })
    const resumed = await find("research_resume").execute({}, {})
    expect(resumed).toEqual({ result: "RUNNING" })
    const decide = vi.spyOn(ctx.researchSupervisor, "setPhaseDecision").mockImplementation(async (decision) => {
      ctx.researchSupervisor.state.status = "COMPLETED"
      return { decision }
    })
    expect(await find("research_validate").execute({}, {})).toEqual({ status: "COMPLETED" })
    expect(decide).toHaveBeenCalledTimes(1)
    expect(decide).toHaveBeenCalledWith("VALIDATE")
    decide.mockRestore()
  })

  it("PREPARE creates the same stable experiment workspaces as Python athena", async () => {
    tmp = mkdtempSync(join(tmpdir(), "athena-dsh-prepare-"))
    const createCalls: Array<{ base: string; branch: string; name?: string }> = []
    const edaPath = join(tmp, "workspaces", "eda")
    const statePath = join(tmp, ".athena", "state.json")
    const fakeCtx = {
      get(name: string) {
        if (name === "subagents") return undefined
        return undefined
      },
      researchState: parseResearchState({
        status: "RUNNING",
        phase: "PREPARE",
        search_limit: 10,
        concurrency: 4,
      }),
      researchGit: {
        async init() {
          return "c0"
        },
        async create(base: string, branch: string, opts?: { name?: string }) {
          createCalls.push({ base, branch, name: opts?.name })
          return { path: edaPath, branch, base_commit: base }
        },
      },
    }

    const workers = dshWorkers(fakeCtx as never, {
      parent: {} as never,
      signal: new AbortController().signal,
      task: "predict titanic survival",
      direction: "maximize",
      projectRoot: tmp,
      statePath,
      execution: {} as never,
      runPlanTurn: async () => {
        throw new Error("not used")
      },
    })

    // 缺 DSH subagents 会在 evaluator 阶段失败；但 git/EDA 目录已经按 Python 结构建好。
    await expect(workers.runPreparePhase()).rejects.toThrow("DSH subagents service is not available")

    expect(createCalls).toEqual([{ base: "c0", branch: "athena/prepare", name: "eda" }])
    expect(fakeCtx.researchState.eda_dir).toBe(relative(tmp, edaPath) || ".")
    expect(JSON.parse(readFileSync(statePath, "utf-8"))["eda_dir"]).toBe(
      relative(tmp, edaPath) || "."
    )
  })
})
