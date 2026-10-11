import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderUi } from "../../../test/render";
import { createEmptyPipelineViewModel } from "../../../types/ui";
import { RunControls } from "../RunControls";

function renderControls(manual: boolean) {
  const onToggleMode = vi.fn();
  renderUi(
    <RunControls
      viewModel={{ ...createEmptyPipelineViewModel(), manual }}
      active={false}
      onPause={vi.fn()}
      onResume={vi.fn()}
      onStop={vi.fn()}
      onToggleMode={onToggleMode}
    />,
  );
  return onToggleMode;
}

describe("RunControls mode switch", () => {
  it("marks the current hypothesis selection mode as pressed", () => {
    renderControls(true);
    expect(screen.getByRole("button", { name: "手动" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "自动" })).toHaveAttribute("aria-pressed", "false");
  });

  it("toggles once when switching from automatic to manual", () => {
    const onToggleMode = renderControls(false);
    fireEvent.click(screen.getByRole("button", { name: "手动" }));
    expect(onToggleMode).toHaveBeenCalledTimes(1);
  });

  it("does not toggle when the already-selected mode is clicked again", () => {
    const onToggleMode = renderControls(false);
    fireEvent.click(screen.getByRole("button", { name: "自动" }));
    expect(onToggleMode).not.toHaveBeenCalled();
  });
});
