import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { ErrorBoundary } from "../common/ErrorBoundary";
import { ThemeToggle } from "../shell/ThemeToggle";

afterEach(() => vi.restoreAllMocks());

describe("shared component contracts", () => {
  it("shows children while rendering succeeds", () => {
    renderUi(<ErrorBoundary><p>正常内容</p></ErrorBoundary>);
    expect(screen.getByText("正常内容")).toBeInTheDocument();
  });

  it("contains a panel failure and displays its error", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    function BrokenPanel(): never { throw new Error("panel unavailable"); }
    renderUi(<ErrorBoundary><BrokenPanel /></ErrorBoundary>);
    expect(screen.getByRole("heading", { name: "面板加载失败" })).toBeInTheDocument();
    expect(screen.getByText("panel unavailable")).toBeInTheDocument();
  });

  it("uses the shared icon style and persists both theme transitions", () => {
    localStorage.setItem("athena-theme", "light");
    renderUi(<ThemeToggle />);
    const button = screen.getByRole("button", { name: "主题：深色" });
    expect(button).toHaveClass("icon-btn");
    fireEvent.click(button);
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("athena-theme")).toBe("dark");
    fireEvent.click(screen.getByRole("button", { name: "主题：浅色" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("athena-theme")).toBe("light");
  });
});
