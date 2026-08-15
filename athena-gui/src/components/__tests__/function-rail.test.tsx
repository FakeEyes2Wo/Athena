import { describe, expect, it, vi } from "vitest";
import { screen, fireEvent } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { FunctionRail } from "../shell/FunctionRail";

describe("FunctionRail", () => {
  it("selects a module and reflects the active state", () => {
    const onSelect = vi.fn();

    renderUi(<FunctionRail module="session" onSelect={onSelect} />);

    const researchTree = screen.getByRole("button", { name: "研究树" });
    fireEvent.click(researchTree);

    expect(onSelect).toHaveBeenCalledWith("research-tree");
  });

  it("marks the active module", () => {
    renderUi(<FunctionRail module="experiments" onSelect={vi.fn()} />);

    expect(screen.getByRole("button", { name: "实验" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "会话" })).not.toHaveAttribute("aria-current");
  });
});
