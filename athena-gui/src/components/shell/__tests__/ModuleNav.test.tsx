import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderUi } from "../../../test/render";
import { ModuleNav } from "../ModuleNav";

describe("ModuleNav", () => {
  it("selects a module", () => {
    const onSelect = vi.fn();
    renderUi(<ModuleNav module="session" onSelect={onSelect} />);

    fireEvent.click(screen.getByRole("button", { name: "研究树" }));

    expect(onSelect).toHaveBeenCalledWith("research-tree");
  });

  it("marks only the active module as the current page", () => {
    renderUi(<ModuleNav module="experiments" onSelect={vi.fn()} />);

    expect(screen.getByRole("button", { name: "实验" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "会话" })).not.toHaveAttribute("aria-current");
  });

  it("shows a counter only when the count is positive", () => {
    const { rerender } = renderUi(
      <ModuleNav module="session" counts={{ experiments: 3 }} onSelect={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: /^实验/ })).toHaveTextContent("3");

    rerender(<ModuleNav module="session" counts={{ experiments: 0 }} onSelect={vi.fn()} />);
    expect(screen.getByRole("button", { name: "实验" })).toBeInTheDocument();
  });
});
