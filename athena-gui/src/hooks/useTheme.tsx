import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

const STORAGE_KEY = "athena-theme";

export type Theme = "light" | "dark";

interface ThemeContextValue {
  mode: Theme;
  cycleMode(): void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function systemPrefersDark(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

/** 读取已保存的主题；缺失或非法时回落到系统偏好。 */
export function getStoredTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return systemPrefersDark() ? "dark" : "light";
}

/** 把主题写到 <html>：Primer 主题 CSS 按这三个属性生效，页面背景随之切换。 */
export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.setAttribute("data-color-mode", theme);
  root.setAttribute("data-light-theme", "light");
  root.setAttribute("data-dark-theme", "dark");
}

// 在 React 挂载前应用已保存/系统主题，避免首屏闪烁。
applyTheme(getStoredTheme());

/** 亮/暗偏好上下文：持久化到 localStorage 并同步到 <html>。 */
export function ThemeModeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<Theme>(getStoredTheme);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, mode);
    applyTheme(mode);
  }, [mode]);

  const cycleMode = useCallback(() => {
    setMode((prev) => (prev === "light" ? "dark" : "light"));
  }, []);

  return <ThemeContext.Provider value={{ mode, cycleMode }}>{children}</ThemeContext.Provider>;
}

/** 当前亮/暗模式与切换函数；必须在 ThemeModeProvider 内使用。 */
export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) {
    throw new Error("useTheme 必须在 ThemeModeProvider 内使用");
  }
  return value;
}
