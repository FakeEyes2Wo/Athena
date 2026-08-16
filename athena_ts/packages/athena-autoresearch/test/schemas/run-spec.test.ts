import { describe, expect, it } from "vitest"
import {
  DEFAULT_RUN_SPEC,
  mergeRunSpecs,
  parseRunSpec,
  RunSpecSchema,
} from "../../src/index.js"

describe("RunSpecSchema", () => {
  it("accepts the default run spec", () => {
    expect(parseRunSpec(DEFAULT_RUN_SPEC).stages.map((stage) => stage.id)).toEqual([
      "intake",
      "ideation",
      "experiment",
      "writing",
      "refinement",
      "packaging",
    ])
  })

  it("rejects an empty stage list", () => {
    const result = RunSpecSchema.safeParse({ ...DEFAULT_RUN_SPEC, stages: [] })
    expect(result.success).toBe(false)
  })

  it("rejects an unknown provider shape", () => {
    const result = RunSpecSchema.safeParse({
      ...DEFAULT_RUN_SPEC,
      providers: { ...DEFAULT_RUN_SPEC.providers, experiment_engine: { id: "" } },
    })
    expect(result.success).toBe(false)
  })

  it("merges run spec overrides deeply", () => {
    const merged = mergeRunSpecs(DEFAULT_RUN_SPEC, {
      run_id: "ar_override",
      budgets: { max_ideas: 5 },
      paper_spec: { title: "Overridden" },
    })
    expect(merged.run_id).toBe("ar_override")
    expect(merged.budgets.max_ideas).toBe(5)
    expect(merged.budgets.max_experiments).toBe(DEFAULT_RUN_SPEC.budgets.max_experiments)
    expect(merged.paper_spec.title).toBe("Overridden")
  })
})
