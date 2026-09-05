import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import fs from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import { parseOrThrow } from "@athena/core"
import * as core from "@athena/core"
import { PlanStateSchema } from "../../src/supervisor/plans.js"
import { parseResearchState, loadResearchState, saveResearchState, researchStateToJSON, type ResearchState } from "../../src/supervisor/state.js"

const TRUSTED_REF = "sha256:" + "b".repeat(64)
const CONTEXT_REF = "sha256:" + "d".repeat(64)

function searchState(): ResearchState {
  return parseResearchState({
    status: "RUNNING",
    phase: "SEARCH",
    search_limit: 10,
    concurrency: 4,
    plans: {
      hyp_vit: parseOrThrow(PlanStateSchema, {
        kind: "SEARCH",
        context_ref: CONTEXT_REF,
        turns_used: 2,
        turn_limit: 12,
        patience: 4,
        stale_rounds: 1,
        best_ref: TRUSTED_REF,
      }),
    },
  })
}

let tmp: string
afterEach(() => {
  if (tmp) rmSync(tmp, { recursive: true, force: true })
  vi.restoreAllMocks()
})

function tmpDir(): string {
  tmp = mkdtempSync(join(tmpdir(), "athena-state-"))
  return tmp
}

describe("ResearchState", () => {
  it("parses plain data with detached plan snapshots and independent defaults", () => {
    const source = searchState()
    const copy = parseResearchState(source)
    expect(Object.getPrototypeOf(copy)).toBe(Object.prototype)
    source.plans["hyp_vit"]!.turns_used = 99
    expect(copy.plans["hyp_vit"]!.turns_used).toBe(2)
    const input = { status: "RUNNING", phase: "PREPARE", search_limit: 1, concurrency: 1 }
    const first = parseResearchState(input)
    const second = parseResearchState(input)
    first.plans["hyp_vit"] = copy.plans["hyp_vit"]!
    expect(second.plans).toEqual({})
  })

  it("loads through a single validation pass", () => {
    const path = join(tmpDir(), "state.json")
    saveResearchState(path, searchState())
    const parse = vi.spyOn(core, "parseOrThrow")
    const loaded = loadResearchState(path)
    expect(loaded.plans["hyp_vit"]!.turns_used).toBe(2)
    expect(parse).toHaveBeenCalledTimes(1)
  })

  it("projects only durable fields while keeping current mutable state", () => {
    const state = searchState()
    state.ideator_count = 5
    state.hypotheses_per_ideator = 4
    state.eda_dir = "workspaces/eda"
    state.task_understanding = { title: "task" }
    state.validation = { report_ref: TRUSTED_REF }
    Object.assign(state, { transient: "not persisted" })
    const json = researchStateToJSON(state)
    expect(json).not.toHaveProperty("transient")
    expect(json).toMatchObject({
      ideator_count: 5, hypotheses_per_ideator: 4, eda_dir: "workspaces/eda",
      task_understanding: { title: "task" }, validation: { report_ref: TRUSTED_REF },
    })
    expect(Object.keys(json)).toHaveLength(11)
  })

  it("search plan round trips with exact durable fields", () => {
    const dir = tmpDir()
    const path = join(dir, "state.json")
    const state = searchState()

    const savedPath = saveResearchState(path, state)

    expect(savedPath).toBe(path)
    expect(loadResearchState(path)).toEqual(state)
    const planJson = JSON.parse(readFileSync(path, "utf-8"))["plans"]["hyp_vit"]
    expect(Object.keys(planJson).sort()).toEqual([
      "best_ref",
      "context_ref",
      "kind",
      "patience",
      "stale_rounds",
      "turn_limit",
      "turns_used",
    ])
  })

  it.each([
    ["prepare", "PREPARE"],
    ["validate", "VALIDATE"],
  ])("non-search plans omit search-only fields (%s)", (planId, kind) => {
    const dir = tmpDir()
    const path = join(dir, "state.json")
    const state = parseResearchState({
      status: "RUNNING",
      phase: kind as "PREPARE" | "VALIDATE",
      search_limit: 10,
      concurrency: 4,
      plans: {
        [planId]: parseOrThrow(PlanStateSchema, {
          kind,
          context_ref: CONTEXT_REF,
          turns_used: 0,
          turn_limit: 12,
        }),
      },
    })

    saveResearchState(path, state)

    const planJson = JSON.parse(readFileSync(path, "utf-8"))["plans"][planId]
    expect(Object.keys(planJson).sort()).toEqual([
      "context_ref",
      "kind",
      "turn_limit",
      "turns_used",
    ])
  })

  it.each([
    ["prepare", "SEARCH"],
    ["validate", "SEARCH"],
    ["hyp_vit", "PREPARE"],
    ["hyp_vit", "VALIDATE"],
  ])("plan map keys must match plan kind (%s/%s)", (planId, kind) => {
    const planPayload: Record<string, unknown> = {
      kind,
      context_ref: CONTEXT_REF,
      turns_used: 0,
      turn_limit: 12,
    }
    if (kind === "SEARCH") planPayload["patience"] = 4

    expect(() =>
      parseResearchState({
        status: "RUNNING",
        phase: "SEARCH",
        search_limit: 10,
        concurrency: 4,
        plans: { [planId]: parseOrThrow(PlanStateSchema, planPayload) },
      })
    ).toThrow(/plan key/)
  })

  it.each([
    ["search_limit", -1],
    ["concurrency", 0],
  ])("rejects invalid limits (%s)", (field, value) => {
    const payload: Record<string, unknown> = {
      status: "RUNNING",
      phase: "SEARCH",
      search_limit: 10,
      concurrency: 4,
      plans: {},
      [field]: value,
    }
    expect(() => parseResearchState(payload as never)).toThrow()
  })

  it.each(["search_limit", "concurrency"])("rejects coerced numeric limits (%s)", (field) => {
    expect(() =>
      parseResearchState({
        status: "RUNNING",
        phase: "SEARCH",
        search_limit: 10,
        concurrency: 4,
        plans: {},
        [field]: "4",
      } as never)
    ).toThrow()
  })

  it.each(["", " ", "\t"])("search plan key must be nonblank (%j)", (planId) => {
    expect(() =>
      parseResearchState({
        status: "RUNNING",
        phase: "SEARCH",
        search_limit: 10,
        concurrency: 4,
        plans: {
          [planId]: parseOrThrow(PlanStateSchema, {
            kind: "SEARCH",
            context_ref: CONTEXT_REF,
            turns_used: 0,
            turn_limit: 12,
            patience: 4,
          }),
        },
      })
    ).toThrow(/Hypothesis ID/)
  })

  it("load accepts non-search plan with search fields omitted", () => {
    const dir = tmpDir()
    const path = join(dir, "state.json")
    writeFileSync(
      path,
      JSON.stringify({
        status: "RUNNING",
        phase: "PREPARE",
        search_limit: 10,
        concurrency: 4,
        plans: {
          prepare: {
            kind: "PREPARE",
            context_ref: CONTEXT_REF,
            turns_used: 0,
            turn_limit: 12,
          },
        },
        validation: null,
      }),
      "utf-8"
    )

    const loaded = loadResearchState(path)
    expect(loaded.plans["prepare"]!.kind).toBe("PREPARE")
  })

  it("rejects unknown status and phase", () => {
    expect(() =>
      parseResearchState({
        status: "PAUSED",
        phase: "IDLE",
        search_limit: 10,
        concurrency: 4,
      } as never)
    ).toThrow()
  })

  it("save atomically replaces existing state", () => {
    const dir = tmpDir()
    const path = join(dir, "state.json")
    writeFileSync(path, '{"old": true}\n', "utf-8")

    saveResearchState(path, searchState())

    expect(loadResearchState(path)).toEqual(searchState())
    expect(existsSync(join(dir, "state.json.tmp"))).toBe(false)
  })

  it("save removes temporary file when replace fails", () => {
    const dir = tmpDir()
    const path = join(dir, "state.json")
    const original = '{"old": true}\n'
    writeFileSync(path, original, "utf-8")

    const rename = vi.spyOn(fs, "renameSync").mockImplementation(() => {
      throw new Error("replace failed")
    })

    expect(() => saveResearchState(path, searchState())).toThrow(/replace failed/)

    expect(readFileSync(path, "utf-8")).toBe(original)
    expect(existsSync(join(dir, "state.json.tmp"))).toBe(false)
    rename.mockRestore()
  })

  it("load rejects non-object json", () => {
    const dir = tmpDir()
    const path = join(dir, "state.json")
    writeFileSync(path, "[]", "utf-8")

    expect(() => loadResearchState(path)).toThrow(/must be an object/)
  })
})
