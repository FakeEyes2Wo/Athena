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

  it("toggles the theme from the shared icon button and persists both transitions", () => {
    localStorage.setItem("athena-theme", "light");
    renderUi(<ThemeToggle />);
    fireEvent.click(screen.getByRole("button", { name: "主题：深色" }));
    expect(document.documentElement.getAttribute("data-color-mode")).toBe("dark");
    expect(localStorage.getItem("athena-theme")).toBe("dark");
    fireEvent.click(screen.getByRole("button", { name: "主题：浅色" }));
    expect(document.documentElement.getAttribute("data-color-mode")).toBe("light");
    expect(localStorage.getItem("athena-theme")).toBe("light");
  });
});
