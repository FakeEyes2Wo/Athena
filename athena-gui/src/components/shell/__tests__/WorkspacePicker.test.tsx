import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WorkspacePicker } from "../WorkspacePicker";

function renderPicker(
  overrides: Partial<React.ComponentProps<typeof WorkspacePicker>> = {},
) {
  const props: React.ComponentProps<typeof WorkspacePicker> = {
    currentRoot: "C:/current",
    recentRoots: [],
    error: null,
    switching: false,
    browsing: false,
    onSelect: vi.fn(),
    onBrowse: vi.fn(),
    onContinue: vi.fn(),
    ...overrides,
  };
  return { ...render(<WorkspacePicker {...props} />), props };
}

describe("WorkspacePicker", () => {
  it("opens the native directory browser", () => {
    const { props } = renderPicker();

    fireEvent.click(screen.getByRole("button", { name: "浏览目录…" }));

    expect(props.onBrowse).toHaveBeenCalledOnce();
  });

  it.each([
    { browsing: true, switching: false, label: "浏览中…" },
    { browsing: false, switching: true, label: "浏览目录…" },
  ])("disables native browsing while work is in flight", ({ label, ...state }) => {
    renderPicker(state);

    expect(screen.getByRole("button", { name: label })).toBeDisabled();
  });

  it("keeps a manually entered path when native browsing is cancelled", () => {
    renderPicker({ onBrowse: vi.fn().mockResolvedValue(undefined) });
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "D:/manual" } });

    fireEvent.click(screen.getByRole("button", { name: "浏览目录…" }));

    expect(input).toHaveValue("D:/manual");
  });

  it("still submits a manually entered absolute path", () => {
    const { props } = renderPicker();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "/home/athena/project" },
    });

    fireEvent.click(screen.getByRole("button", { name: "打开" }));

    expect(props.onSelect).toHaveBeenCalledWith("/home/athena/project");
  });
});
