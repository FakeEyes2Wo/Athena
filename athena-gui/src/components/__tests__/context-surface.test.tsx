import { describe, expect, it, vi } from "vitest";
import { screen, fireEvent } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { ContextSurface } from "../context/ContextSurface";
import { createEmptyPipelineViewModel } from "../../types/ui";

describe("ContextSurface", () => {
  it("renders the active panel when opened and closes on demand", () => {
    const closePanel = vi.fn();

    renderUi(
      <ContextSurface
        viewModel={{
          ...createEmptyPipelineViewModel(),
          contextSurface: { isOpen: true, activePanel: "metrics" },
        }}
        closePanel={closePanel}
      />,
    );

    const headings = screen.getAllByRole("heading", { name: /指标/i });
    expect(headings.length).toBeGreaterThanOrEqual(1);
    fireEvent.click(screen.getByRole("button", { name: /关闭/i }));
    expect(closePanel).toHaveBeenCalledTimes(1);
  });

  it("shows a collapsed state when the context surface is closed", () => {
    renderUi(
      <ContextSurface
        viewModel={createEmptyPipelineViewModel()}
        closePanel={vi.fn()}
      />,
    );

    expect(screen.getByText("详情已收起")).toBeInTheDocument();
  });
});
