import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AppProviders } from "../../components/common/AppProviders";
import { ThemeToggle } from "../../components/shell/ThemeToggle";
import { getStoredTheme, useTheme } from "../useTheme";

function stubSystemDark(matches: boolean) {
  vi.spyOn(window, "matchMedia").mockImplementation((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

describe("useTheme", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("syncs Primer color-mode attributes on <html> and the ThemeProvider, and persists the choice", () => {
    localStorage.setItem("athena-theme", "light");
    render(
      <AppProviders>
        <ThemeToggle />
      </AppProviders>,
    );

    const root = document.documentElement;
    expect(root.getAttribute("data-color-mode")).toBe("light");
    expect(root.getAttribute("data-light-theme")).toBe("light");
    expect(root.getAttribute("data-dark-theme")).toBe("dark");

    fireEvent.click(screen.getByRole("button", { name: "主题：深色" }));

    expect(root.getAttribute("data-color-mode")).toBe("dark");
    expect(localStorage.getItem("athena-theme")).toBe("dark");
    expect(document.querySelector('[data-component="ThemeProvider"]')).toHaveAttribute("data-color-mode", "dark");
    expect(screen.getByRole("button", { name: "主题：浅色" })).toBeInTheDocument();
  });

  it("falls back to the system preference when the stored value is missing or invalid", () => {
    localStorage.setItem("athena-theme", "purple");
    stubSystemDark(true);
    expect(getStoredTheme()).toBe("dark");

    localStorage.removeItem("athena-theme");
    stubSystemDark(false);
    expect(getStoredTheme()).toBe("light");
  });

  it("throws a clear error when used outside ThemeModeProvider", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    function Orphan() {
      useTheme();
      return null;
    }
    expect(() => render(<Orphan />)).toThrow(/ThemeModeProvider/);
  });
});
