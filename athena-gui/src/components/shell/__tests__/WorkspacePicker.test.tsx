import { act, fireEvent, render, screen } from "@testing-library/react";
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

  it("disables native browsing only while the directory dialog is open", () => {
    renderPicker({ browsing: true });

    expect(screen.getByRole("button", { name: "浏览中…" })).toBeDisabled();
  });

  it("keeps switch targets usable while switching and disables only Continue", () => {
    const { props } = renderPicker({
      recentRoots: ["C:/recent"],
      switching: true,
    });

    const recentRoot = screen.getByRole("button", { name: "C:/recent" });
    const browse = screen.getByRole("button", { name: "浏览目录…" });
    const input = screen.getByRole("textbox");
    const continueButton = screen.getByRole("button", { name: "继续使用当前" });

    expect(recentRoot).not.toBeDisabled();
    expect(browse).not.toBeDisabled();
    expect(input).not.toBeDisabled();
    expect(continueButton).toBeDisabled();

    fireEvent.click(recentRoot);
    fireEvent.click(browse);
    fireEvent.change(input, { target: { value: "D:/queued" } });
    const open = screen.getByRole("button", { name: "打开" });
    expect(open).not.toBeDisabled();
    fireEvent.click(open);

    expect(props.onSelect).toHaveBeenNthCalledWith(1, "C:/recent");
    expect(props.onSelect).toHaveBeenNthCalledWith(2, "D:/queued");
    expect(props.onBrowse).toHaveBeenCalledOnce();
    expect(props.onContinue).not.toHaveBeenCalled();
  });

  it("keeps a manually entered path while native browsing is cancelled", async () => {
    let cancelBrowse!: () => void;
    const onBrowse = vi.fn(
      () => new Promise<null>((resolve) => {
        cancelBrowse = () => resolve(null);
      }),
    );
    const view = renderPicker({ onBrowse });
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "D:/manual" } });

    fireEvent.click(screen.getByRole("button", { name: "浏览目录…" }));
    view.rerender(<WorkspacePicker {...view.props} browsing />);

    expect(onBrowse).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "浏览中…" })).toBeDisabled();
    expect(input).toHaveValue("D:/manual");

    await act(async () => cancelBrowse());
    view.rerender(<WorkspacePicker {...view.props} browsing={false} />);

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
