import { describe, expect, it } from "vitest"
import { DataScriptBundleSchema, ValidationResultSchema } from "../src/contracts.js"

describe("research artifact contracts", () => {
  it("retains incomplete bundle defaults for explicit freeze validation", () => {
    expect(DataScriptBundleSchema.parse({ bundle_id: "b", entrypoint: "eval.py" })).toEqual({
      bundle_id: "b", entrypoint: "eval.py", runtime: "python-uv",
      lock_ref: null, project_ref: null, source_ref: null, tree_ref: null,
      python_version: null, environment_hash: null,
    })
  })

  it("preserves frozen bundle metadata through JSON", () => {
    const bundle = {
      bundle_id: "b", entrypoint: "nested/eval.py", runtime: "python-uv",
      lock_ref: "lock", project_ref: "project", source_ref: "source", tree_ref: "tree",
      python_version: "3.11.0", environment_hash: "environment",
    }
    expect(DataScriptBundleSchema.parse(JSON.parse(JSON.stringify(bundle)))).toEqual(bundle)
  })

  it("projects legacy validation payloads to the six consumed fields", () => {
    expect(ValidationResultSchema.parse({
      result_id: "v", status: "COMPLETED", test_score: 0.8, final_test_score: 0.7,
      generalization_gap: 0.1, generalization_warning: true,
      sota_commit: null, validation_commit: null, predictions_ref: null,
      predictions_path: null, evidence_ref: null,
    })).toEqual({
      result_id: "v", status: "COMPLETED", test_score: 0.8, final_test_score: 0.7,
      generalization_gap: 0.1, generalization_warning: true,
    })
    expect(ValidationResultSchema.parse({ result_id: "v", status: "FAILED" })).toEqual({
      result_id: "v", status: "FAILED", test_score: null, final_test_score: null,
      generalization_gap: null, generalization_warning: false,
    })
  })
})
