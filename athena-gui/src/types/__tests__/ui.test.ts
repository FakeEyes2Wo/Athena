import { describe, expect, it } from "vitest";
import { CONTEXT_PANELS, createEmptyPipelineViewModel } from "../../types/ui";

describe("ui model bootstrap", () => {
  it("defines the supported context panels and an idle default view model", () => {
    expect(CONTEXT_PANELS).toEqual([
      "metrics",
      "research-tree",
      "experiment-log",
      "diff",
      "files",
      "eda-report",
      "report",
      "hypothesis-graph",
      "algorithms",
      "settings",
      "llm-io",
      "experiments",
    ]);

    expect(createEmptyPipelineViewModel()).toMatchObject({
      phase: "idle",
      status: "idle",
      messages: [],
    });
  });
});
