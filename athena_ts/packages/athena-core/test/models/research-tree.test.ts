import fs from "node:fs"
import { mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { fileURLToPath } from "node:url"
import { afterEach, describe, expect, it, vi } from "vitest"
import { parseOrThrow } from "../../src/errors.js"
import {
  ComparisonVerdictSchema,
  EvalResultSchema,
  HypothesisSchema,
} from "../../src/models/research-models.js"
import type { EvalResult, Hypothesis } from "../../src/models/research-models.js"
import {
  ExperimentSchema,
  ResearchTree,
} from "../../src/models/research-tree.js"
import type { Experiment } from "../../src/models/research-tree.js"

const FIXTURE = fileURLToPath(
  new URL("../fixtures/research_tree_v2.json", import.meta.url),
)

function makeHypothesis(
  hypothesisId: string,
  parentId: string | null = null,
): Hypothesis {
  return parseOrThrow(HypothesisSchema, {
    id: hypothesisId,
    parent_id: parentId,
    statement: `Hypothesis ${hypothesisId}`,
    intervention: `Apply intervention ${hypothesisId}`,
    expected_effect: "Improve the primary metric",
    sources: ["common_knowledge"],
  })
}

function makeExperiment(
  hypothesisId: string,
  opts: {
    parentId?: string | null
    kind?: string
    commit?: string
  } = {},
): Experiment {
  const parentId = opts.parentId ?? null
  const kind = opts.kind ?? "search"
  const commit = opts.commit ?? "a".repeat(40)
  return parseOrThrow(ExperimentSchema, {
    parent_id: parentId,
    hypothesis_id: hypothesisId,
    commit,
    plan: {
      kind,
      change: `Execute ${hypothesisId}`,
      run_config_ref: `artifact://runs/${hypothesisId}/config`,
      budget: { trials: 1 },
      acceptance_rule: "The frozen evaluator completes",
    },
    gitwork: {
      path: `C:/worktrees/${hypothesisId}`,
      branch: `experiment/${hypothesisId}`,
      base_commit: commit,
    },
  })
}

function successfulEval(experimentId: string, primary = 0.75): EvalResult {
  return parseOrThrow(EvalResultSchema, {
    experiment_id: experimentId,
    primary,
    secondary: {},
    per_sample: `artifact://samples/${experimentId}`,
  })
}

function complete(
  tree: ResearchTree,
  experimentId: string,
  primary = 0.75,
): void {
  tree.transitionExperiment(experimentId, "RUNNING")
  tree.completeExperiment(experimentId, {
    eval: successfulEval(experimentId, primary),
    verdict: null,
    artifacts: {
      diff: `artifact://diffs/${experimentId}`,
      logs: `artifact://logs/${experimentId}`,
    },
  })
}

describe("ResearchTree v2 graph", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("experiment has only canonical v2 fields", () => {
    expect(Object.keys(ExperimentSchema.shape)).toEqual([
      "parent_id",
      "hypothesis_id",
      "commit",
      "plan",
      "gitwork",
      "status",
      "eval",
      "verdict",
      "artifacts",
      "error",
    ])
    expect("id" in ExperimentSchema.shape).toBe(false)
    expect("hypothesis" in ExperimentSchema.shape).toBe(false)
  })

  it("graph owns hypotheses once and derives breadth-first children", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(makeHypothesis("hyp_root"))
    tree.addExperiment("exp_root", makeExperiment("hyp_root", { kind: "baseline" }))
    tree.addHypothesis(makeHypothesis("hyp_left", "exp_root"))
    tree.addExperiment("exp_left", makeExperiment("hyp_left", { parentId: "exp_root" }))
    tree.addHypothesis(makeHypothesis("hyp_right", "exp_root"))
    tree.addExperiment("exp_right", makeExperiment("hyp_right", { parentId: "exp_root" }))
    tree.addHypothesis(makeHypothesis("hyp_leaf", "exp_left"))
    tree.addExperiment("exp_leaf", makeExperiment("hyp_leaf", { parentId: "exp_left" }))

    expect(tree.getHypothesis("hyp_root").id).toBe("hyp_root")
    expect(tree.getHypothesis("hyp_root").order).toBe(0)
    expect(tree.getExperiment("exp_root").hypothesis_id).toBe("hyp_root")
    expect(tree.rootExperimentIds()).toEqual(["exp_root"])
    expect(tree.listChildren("exp_root")).toEqual(["exp_left", "exp_right"])
    expect(tree.listDescendants("exp_root")).toEqual([
      "exp_left",
      "exp_right",
      "exp_leaf",
    ])
    expect(tree.experimentPath("exp_leaf")).toEqual([
      "exp_root",
      "exp_left",
      "exp_leaf",
    ])
    expect(tree.hypothesesPath("exp_leaf").map((h) => h.id)).toEqual([
      "hyp_root",
      "hyp_left",
      "hyp_leaf",
    ])
  })

  it("graph rejects duplicate and missing references without mutation", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(makeHypothesis("hyp_root"))
    tree.addExperiment("exp_root", makeExperiment("hyp_root", { kind: "baseline" }))
    const before = tree.toDict()

    expect(() =>
      tree.addExperiment("exp_root", makeExperiment("hyp_root")),
    ).toThrow(/duplicate experiment id/)
    expect(() =>
      tree.addExperiment("exp_missing_hyp", makeExperiment("hyp_missing")),
    ).toThrow(/hyp_missing/)
    expect(() =>
      tree.addHypothesis(makeHypothesis("hyp_orphan", "exp_missing")),
    ).toThrow(/exp_missing/)
    expect(() => tree.addHypothesis(makeHypothesis("hyp_root"))).toThrow(
      /duplicate hypothesis id/,
    )

    expect(tree.toDict()).toEqual(before)
  })

  it("status transitions are explicit and terminal", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(makeHypothesis("hyp_root"))
    tree.addExperiment("exp_root", makeExperiment("hyp_root", { kind: "baseline" }))

    tree.transitionExperiment("exp_root", "RUNNING")
    expect(tree.getExperiment("exp_root").status).toBe("RUNNING")

    const verdict = parseOrThrow(ComparisonVerdictSchema, {
      winner: "candidate",
      p_value: 0.01,
    })
    tree.completeExperiment("exp_root", {
      eval: successfulEval("exp_root"),
      verdict,
      artifacts: { logs: "artifact://logs/exp_root" },
    })
    const completed = tree.getExperiment("exp_root")
    expect(completed.status).toBe("SUCCEEDED")
    expect(completed.verdict).toEqual(verdict)
    expect(completed.error).toBe(null)

    expect(() =>
      tree.transitionExperiment("exp_root", "CANCELLED"),
    ).toThrow(/terminal/)
  })

  it("failure and cancellation preserve existing evidence", () => {
    const tree = new ResearchTree()
    for (const hypothesisId of ["hyp_failed", "hyp_cancelled"]) {
      tree.addHypothesis(makeHypothesis(hypothesisId))
    }
    tree.addExperiment("exp_failed", makeExperiment("hyp_failed"))
    tree.addExperiment("exp_cancelled", makeExperiment("hyp_cancelled"))

    tree.attachArtifact("exp_failed", "logs", "artifact://logs/failed")
    tree.transitionExperiment("exp_failed", "RUNNING")
    tree.transitionExperiment("exp_failed", "FAILED", { error: "training failed" })
    tree.transitionExperiment("exp_cancelled", "CANCELLED")

    const failed = tree.getExperiment("exp_failed")
    expect(failed.error).toBe("training failed")
    expect(failed.artifacts["logs"]).toBe("artifact://logs/failed")
    expect(tree.getExperiment("exp_cancelled").status).toBe("CANCELLED")

    expect(() => {
      const pending = new ResearchTree()
      pending.addHypothesis(makeHypothesis("hyp_pending"))
      pending.addExperiment("exp_pending", makeExperiment("hyp_pending"))
      pending.transitionExperiment("exp_pending", "RUNNING")
      pending.transitionExperiment("exp_pending", "FAILED")
    }).toThrow(/nonblank error/)
  })

  it("completion requires matching finite real evaluation", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(makeHypothesis("hyp_root"))
    tree.addExperiment("exp_root", makeExperiment("hyp_root", { kind: "baseline" }))
    tree.transitionExperiment("exp_root", "RUNNING")

    expect(() =>
      tree.completeExperiment("exp_root", {
        eval: successfulEval("another"),
        verdict: null,
        artifacts: {},
      }),
    ).toThrow(/experiment id/)
    expect(() =>
      tree.completeExperiment("exp_root", {
        eval: successfulEval("exp_root", Infinity),
        verdict: null,
        artifacts: {},
      }),
    ).toThrow(/finite/)

    expect(tree.getExperiment("exp_root").status).toBe("RUNNING")
  })

  it("sota requires eligible successful experiment", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(makeHypothesis("hyp_baseline"))
    tree.addExperiment(
      "exp_baseline",
      makeExperiment("hyp_baseline", { kind: "baseline" }),
    )
    tree.addHypothesis(makeHypothesis("hyp_validation", "exp_baseline"))
    tree.addExperiment(
      "exp_validation",
      makeExperiment("hyp_validation", {
        parentId: "exp_baseline",
        kind: "ablation",
      }),
    )

    expect(() => tree.setSota("exp_baseline")).toThrow(/successful/)

    complete(tree, "exp_baseline")
    tree.setSota("exp_baseline")
    expect(tree.bestExperimentId()).toBe("exp_baseline")

    complete(tree, "exp_validation", 0.8)
    expect(() => tree.setSota("exp_validation")).toThrow(/eligible/)
    expect(tree.bestExperimentId()).toBe("exp_baseline")
  })
})

describe("ResearchTree load/save", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  function fixturePayload(): Record<string, any> {
    return JSON.parse(readFileSync(FIXTURE, "utf-8"))
  }

  it("v2 fixture loads and migrates with derived children", () => {
    const tree = ResearchTree.load(FIXTURE)

    expect(tree.bestExperimentId()).toBe("exp_baseline")
    expect(tree.listChildren("exp_baseline")).toEqual(["exp_child"])
    expect(JSON.stringify(tree.toDict())).not.toContain("children_ids")

    const dir = mkdtempSync(join(tmpdir(), "athena-tree-"))
    const target = join(dir, "nested", "research_tree.json")
    expect(tree.save(target)).toBe(target)
    const migrated = JSON.parse(readFileSync(target, "utf-8"))
    expect(migrated["version"]).toBe(3)
    expect(migrated["hypotheses"]["hyp_baseline"]["order"]).toBe(0)
    expect(migrated["hypotheses"]["hyp_child"]["order"]).toBe(1)
    expect(ResearchTree.load(target).toDict()).toEqual(tree.toDict())
  })

  for (const version of [null, 1, 4]) {
    it(`load rejects missing/v1/unknown version ${version}`, () => {
      const payload = fixturePayload()
      if (version === null) {
        delete payload["version"]
      } else {
        payload["version"] = version
      }

      expect(() => ResearchTree.fromDict(payload)).toThrow(
        /unsupported research tree version/,
      )
    })
  }

  it("load rejects v1 nodes shape even when version is forged", () => {
    expect(() =>
      ResearchTree.fromDict({
        version: 2,
        sota_id: null,
        nodes: {},
        hypotheses: {},
      }),
    ).toThrow(/top-level fields/)
  })

  it("load rejects missing parents, cycles and unknown hypotheses", () => {
    const missingParent = fixturePayload()
    missingParent["experiments"]["exp_child"]["parent_id"] = "unknown"
    expect(() => ResearchTree.fromDict(missingParent)).toThrow(/unknown/)

    const cycle = fixturePayload()
    cycle["experiments"]["exp_baseline"]["parent_id"] = "exp_child"
    expect(() => ResearchTree.fromDict(cycle)).toThrow(/cycle/)

    const missingHypothesis = fixturePayload()
    missingHypothesis["experiments"]["exp_child"]["hypothesis_id"] = "unknown"
    expect(() => ResearchTree.fromDict(missingHypothesis)).toThrow(/unknown/)
  })

  it("load rejects invalid records and ineligible sota", () => {
    const invalidStatus = fixturePayload()
    invalidStatus["experiments"]["exp_child"]["status"] = "DONE"
    expect(() => ResearchTree.fromDict(invalidStatus)).toThrow(/status/)

    const invalidWorktree = fixturePayload()
    invalidWorktree["experiments"]["exp_child"]["gitwork"]["path"] = ""
    expect(() => ResearchTree.fromDict(invalidWorktree)).toThrow(/worktree/)

    const invalidArtifact = fixturePayload()
    invalidArtifact["experiments"]["exp_child"]["artifacts"]["logs"] = ""
    expect(() => ResearchTree.fromDict(invalidArtifact)).toThrow(/artifacts/)

    const ineligibleSota = fixturePayload()
    ineligibleSota["experiments"]["exp_child"]["plan"]["kind"] = "ablation"
    ineligibleSota["experiments"]["exp_child"]["status"] = "SUCCEEDED"
    ineligibleSota["experiments"]["exp_child"]["eval"] = {
      experiment_id: "exp_child",
      primary: 0.76,
      secondary: {},
      per_sample: "artifact://samples/exp_child",
    }
    ineligibleSota["sota_id"] = "exp_child"
    expect(() => ResearchTree.fromDict(ineligibleSota)).toThrow(/eligible/)
  })

  it("load rejects mismatched hypothesis mapping key", () => {
    const payload = fixturePayload()
    payload["hypotheses"]["hyp_baseline"]["id"] = "hyp_other"

    expect(() => ResearchTree.fromDict(payload)).toThrow(/mapping key/)
  })

  it("failed atomic replace keeps last valid file", () => {
    const dir = mkdtempSync(join(tmpdir(), "athena-tree-"))
    const target = join(dir, "research_tree.json")
    const original = '{"last":"valid"}'
    writeFileSync(target, original, "utf-8")
    vi.spyOn(fs, "renameSync").mockImplementation(() => {
      throw new Error("disk full")
    })

    expect(() => new ResearchTree().save(target)).toThrow(/disk full/)

    expect(readFileSync(target, "utf-8")).toBe(original)
    expect(readdirSync(dir)).toEqual(["research_tree.json"])
  })
})
