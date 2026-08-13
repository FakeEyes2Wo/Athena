import { mkdtempSync, readFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"
import { parseOrThrow } from "../../src/errors.js"
import { HypothesisSchema } from "../../src/models/research-models.js"
import type { Hypothesis } from "../../src/models/research-models.js"
import {
  ExperimentSchema,
  ResearchTree,
} from "../../src/models/research-tree.js"
import type { Experiment } from "../../src/models/research-tree.js"

const LEGACY_V2_FIXTURE = fileURLToPath(
  new URL("../fixtures/research_tree_v2.json", import.meta.url),
)

function _hypothesis(
  hypothesisId: string,
  opts: {
    parentId?: string | null
    supersedes?: string[]
    priority?: number
    order?: number | null
  } = {},
): Hypothesis {
  const parentId = opts.parentId ?? null
  const supersedes = opts.supersedes ?? []
  const priority = opts.priority ?? 1000.0
  const order = opts.order ?? null
  return parseOrThrow(HypothesisSchema, {
    id: hypothesisId,
    parent_id: parentId,
    statement: `claim ${hypothesisId}`,
    intervention: `change ${hypothesisId}`,
    expected_effect: "improve the trusted metric",
    supersedes,
    priority,
    order,
    patience: 5,
    turn_limit: 12,
  })
}

function _experiment(
  hypothesisId: string,
  parentId: string | null = null,
): Experiment {
  return parseOrThrow(ExperimentSchema, {
    parent_id: parentId,
    hypothesis_id: hypothesisId,
    commit: "abc123",
    plan: {
      kind: "search",
      change: `test ${hypothesisId}`,
      run_config_ref: "artifact://run-config",
      budget: {},
      acceptance_rule: "trusted evaluation completes",
    },
    gitwork: {
      path: `C:/worktrees/${hypothesisId}`,
      branch: `search/${hypothesisId}`,
      base_commit: "base123",
    },
  })
}

function _treeWithPath(...hypothesisIds: string[]): ResearchTree {
  const tree = new ResearchTree()
  let parentExperimentId: string | null = null
  for (const hypothesisId of hypothesisIds) {
    tree.addHypothesis(_hypothesis(hypothesisId, { parentId: parentExperimentId }))
    const experimentId = `exp_${hypothesisId.slice(2)}`
    tree.addExperiment(
      experimentId,
      _experiment(hypothesisId, parentExperimentId),
    )
    parentExperimentId = experimentId
  }
  return tree
}

describe("ResearchTree scheduling and selective inheritance", () => {
  it("supersedes only removes named ancestor hypotheses", () => {
    const tree = _treeWithPath("h_data", "h_feature", "h_xgb", "h_depth")
    const child = _hypothesis("h_vit", {
      parentId: "exp_depth",
      supersedes: ["h_xgb", "h_depth"],
      order: 5,
    })

    expect(tree.activeHypotheses("exp_depth", child).map((h) => h.id)).toEqual([
      "h_data",
      "h_feature",
      "h_vit",
    ])
  })

  for (const [supersedes, message] of [
    [["h_xgb", "h_xgb"], "duplicate"],
    [["h_vit"], "child hypothesis"],
    [["h_unrelated"], "selected parent experiment path"],
  ] as const) {
    it(`active_hypotheses rejects invalid supersedes ${JSON.stringify(supersedes)}`, () => {
      const tree = _treeWithPath("h_data", "h_xgb")
      tree.addHypothesis(_hypothesis("h_unrelated"))
      const child = _hypothesis("h_vit", {
        parentId: "exp_xgb",
        supersedes: [...supersedes],
      })

      expect(() => tree.activeHypotheses("exp_xgb", child)).toThrow(
        new RegExp(message),
      )
    })
  }

  it("active_hypotheses rejects a different selected parent", () => {
    const tree = _treeWithPath("h_data", "h_xgb")
    tree.addHypothesis(_hypothesis("h_other"))
    tree.addExperiment("exp_other", _experiment("h_other"))
    const child = _hypothesis("h_vit", { parentId: "exp_xgb" })

    expect(() => tree.activeHypotheses("exp_other", child)).toThrow(
      /parent experiment/,
    )
  })

  it("registration rejects dangling hypothesis parent", () => {
    const tree = new ResearchTree()

    expect(() =>
      tree.addHypothesis(_hypothesis("h_child", { parentId: "exp_missing" })),
    ).toThrow(/unknown parent experiment/)
  })

  it("experiment parent must match hypothesis parent", () => {
    const tree = _treeWithPath("h_root")
    tree.addHypothesis(_hypothesis("h_child", { parentId: "exp_root" }))

    expect(() =>
      tree.addExperiment("exp_child", _experiment("h_child", null)),
    ).toThrow(/does not match hypothesis parent/)
  })

  it("load rejects dangling hypothesis parent", () => {
    const payload = JSON.parse(readFileSync(LEGACY_V2_FIXTURE, "utf-8"))
    payload["hypotheses"]["hyp_child"]["parent_id"] = "exp_missing"

    expect(() => ResearchTree.fromDict(payload)).toThrow(
      /unknown parent experiment/,
    )
  })

  it("load rejects experiment hypothesis parent mismatch", () => {
    const payload = JSON.parse(readFileSync(LEGACY_V2_FIXTURE, "utf-8"))
    payload["hypotheses"]["hyp_child"]["parent_id"] = null

    expect(() => ResearchTree.fromDict(payload)).toThrow(
      /does not match hypothesis parent/,
    )
  })

  it("one hypothesis cannot have two experiments", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(_hypothesis("h1"))
    tree.addExperiment("e1", _experiment("h1"))

    expect(() => tree.addExperiment("e2", _experiment("h1"))).toThrow(
      /already has an experiment/,
    )
  })

  it("loading rejects two experiments for one hypothesis", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(_hypothesis("h1"))
    tree.addExperiment("e1", _experiment("h1"))
    const payload = tree.toDict()
    payload["experiments"]["e2"] = payload["experiments"]["e1"]

    expect(() => ResearchTree.fromDict(payload)).toThrow(
      /already has an experiment/,
    )
  })

  it("experiment_for_hypothesis returns the registered experiment", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(_hypothesis("h1"))
    tree.addExperiment("e1", _experiment("h1"))

    expect(tree.experimentForHypothesis("h1")).toBe("e1")
    expect(tree.experimentForHypothesis("missing")).toBe(null)
  })

  it("registration assigns stable fifo order only when absent", () => {
    const tree = new ResearchTree()

    tree.addHypothesis(_hypothesis("h_explicit", { order: 8 }))
    tree.addHypothesis(_hypothesis("h_first"))
    tree.addHypothesis(_hypothesis("h_second"))

    expect(tree.getHypothesis("h_explicit").order).toBe(8)
    expect(tree.getHypothesis("h_first").order).toBe(9)
    expect(tree.getHypothesis("h_second").order).toBe(10)
  })

  it("registration rejects duplicate explicit order", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(_hypothesis("h_first", { order: 4 }))

    expect(() => tree.addHypothesis(_hypothesis("h_second", { order: 4 }))).toThrow(
      /duplicate hypothesis order/,
    )
  })

  it("load rejects duplicate explicit order", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(_hypothesis("h_first", { order: 4 }))
    tree.addHypothesis(_hypothesis("h_second", { order: 5 }))
    const payload = tree.toDict()
    payload["hypotheses"]["h_second"]["order"] = 4

    expect(() => ResearchTree.fromDict(payload)).toThrow(
      /duplicate hypothesis order/,
    )
  })

  it("v2 payload without scheduling fields loads with compatible defaults", () => {
    const tree = _treeWithPath("h_root")
    const payload = tree.toDict()
    payload["version"] = 2
    const rawHypothesis = payload["hypotheses"]["h_root"]
    for (const field of ["supersedes", "priority", "order", "patience", "turn_limit"]) {
      delete rawHypothesis[field]
    }

    const loaded = ResearchTree.fromDict(payload)
    const hypothesis = loaded.getHypothesis("h_root")

    expect(hypothesis.supersedes).toEqual([])
    expect(hypothesis.priority).toBe(1000.0)
    expect(hypothesis.order).toBe(0)
    expect(hypothesis.patience).toBe(0)
    expect(hypothesis.turn_limit).toBe(null)
  })

  it("v2 payload assigns missing orders in mapping order", () => {
    const tree = new ResearchTree()
    tree.addHypothesis(_hypothesis("h_first"))
    tree.addHypothesis(_hypothesis("h_second"))
    const payload = tree.toDict()
    payload["version"] = 2
    delete payload["hypotheses"]["h_first"]["order"]
    delete payload["hypotheses"]["h_second"]["order"]

    const loaded = ResearchTree.fromDict(payload)

    expect(loaded.getHypothesis("h_first").order).toBe(0)
    expect(loaded.getHypothesis("h_second").order).toBe(1)
  })

  it("reordered legacy v2 payload assigns orders in payload order", () => {
    const payload = JSON.parse(readFileSync(LEGACY_V2_FIXTURE, "utf-8"))
    payload["hypotheses"] = {
      hyp_child: payload["hypotheses"]["hyp_child"],
      hyp_baseline: payload["hypotheses"]["hyp_baseline"],
    }

    const loaded = ResearchTree.fromDict(payload)

    expect(loaded.getHypothesis("hyp_child").order).toBe(0)
    expect(loaded.getHypothesis("hyp_baseline").order).toBe(1)
  })

  it("legacy v2 fixture save migrates scheduling fields to v3", () => {
    const original = JSON.parse(readFileSync(LEGACY_V2_FIXTURE, "utf-8"))
    const tree = ResearchTree.load(LEGACY_V2_FIXTURE)
    const dir = mkdtempSync(join(tmpdir(), "athena-tree-"))
    const target = join(dir, "research_tree.json")

    tree.save(target)

    const migrated = JSON.parse(readFileSync(target, "utf-8"))
    expect(migrated["version"]).toBe(3)
    expect(migrated["experiments"]).toEqual(original["experiments"])
    expect(migrated["hypotheses"]["hyp_baseline"]["order"]).toBe(0)
    expect(migrated["hypotheses"]["hyp_child"]["order"]).toBe(1)

    migrated["hypotheses"] = {
      hyp_child: migrated["hypotheses"]["hyp_child"],
      hyp_baseline: migrated["hypotheses"]["hyp_baseline"],
    }
    const reloaded = ResearchTree.fromDict(migrated)
    expect(reloaded.getHypothesis("hyp_baseline").order).toBe(0)
    expect(reloaded.getHypothesis("hyp_child").order).toBe(1)
  })

  for (const priority of [NaN, Infinity, -Infinity]) {
    it(`hypothesis priority must be finite (${priority})`, () => {
      expect(() => _hypothesis("h_poison", { priority })).toThrow(
        /expected number/,
      )
    })
  }

  it("scheduling fields survive tree round trip", () => {
    const tree = _treeWithPath("h_old")
    tree.addHypothesis(
      _hypothesis("h_child", {
        parentId: "exp_old",
        supersedes: ["h_old"],
        priority: 1048.0,
        order: 7,
      }),
    )

    const loaded = ResearchTree.fromDict(tree.toDict()).getHypothesis("h_child")

    expect(loaded.supersedes).toEqual(["h_old"])
    expect(loaded.priority).toBe(1048.0)
    expect(loaded.order).toBe(7)
    expect(loaded.patience).toBe(5)
    expect(loaded.turn_limit).toBe(12)
  })
})
