import { useCallback, useEffect, useState } from "react";

const STORAGE_KEY = "athena-theme";
type Theme = "light" | "dark";

function systemPrefersDark(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

function getStoredTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return systemPrefersDark() ? "dark" : "light";
}

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

// Apply persisted/system theme before React mounts (avoid flash).
applyTheme(getStoredTheme());

/** Two-state theme (light / dark), persisted to localStorage. */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(getStoredTheme);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, theme);
    applyTheme(theme);
  }, [theme]);

  const cycleMode = useCallback(() => {
    setTheme((prev) => (prev === "light" ? "dark" : "light"));
  }, []);

  return { mode: theme, cycleMode };
}
