import { describe, expect, it, vi } from "vitest";
import { screen, fireEvent } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { RightRail } from "../right-rail/RightRail";
import { createEmptyPipelineViewModel } from "../../types/ui";

describe("RightRail", () => {
  it("renders summary cards and opens the report panel", () => {
    const openPanel = vi.fn();

    renderUi(
      <RightRail
        viewModel={{
          ...createEmptyPipelineViewModel(),
          phase: "SEARCH",
          status: "running",
          rightRail: {
            budgetRemaining: 7,
            noImproveStreak: 2,
            bestPrimary: 0.7812,
            latestExperimentId: "exp-4",
          },
        }}
        openPanel={openPanel}
      />,
    );

    expect(screen.getByRole("button", { name: "状态" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "预算" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "打开报告" }));
    expect(openPanel).toHaveBeenCalledWith("report");
  });

  it("opens experiment log and metrics panels", () => {
    const openPanel = vi.fn();

    renderUi(
      <RightRail
        viewModel={createEmptyPipelineViewModel()}
        openPanel={openPanel}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "状态" }));
    expect(openPanel).toHaveBeenCalledWith("experiment-log");

    fireEvent.click(screen.getByRole("button", { name: "预算" }));
    expect(openPanel).toHaveBeenCalledWith("metrics");
  });
});
