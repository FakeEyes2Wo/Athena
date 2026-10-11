import { useCallback, useEffect, useState } from "react";

const STORAGE_KEY = "athena-theme";
type Theme = "light" | "dark";
export type ThemePreference = Theme | "system";

const listeners = new Set<() => void>();

function systemPrefersDark(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function readPreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // Storage unavailable: follow the system.
  }
  return "system";
}

function resolve(preference: ThemePreference): Theme {
  if (preference === "system") return systemPrefersDark() ? "dark" : "light";
  return preference;
}

function apply(preference: ThemePreference) {
  document.documentElement.dataset.theme = resolve(preference);
}

function writePreference(preference: ThemePreference) {
  try {
    if (preference === "system") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, preference);
  } catch {
    // Non-fatal: the choice still applies for this session.
  }
  apply(preference);
  listeners.forEach((listener) => listener());
}

// Apply persisted/system theme before React mounts (avoid a flash).
apply(readPreference());

if (typeof window.matchMedia === "function") {
  const query = window.matchMedia("(prefers-color-scheme: dark)");
  const onChange = () => {
    if (readPreference() === "system") {
      apply("system");
      listeners.forEach((listener) => listener());
    }
  };
  query.addEventListener?.("change", onChange);
}

/** Theme preference (light / dark / system), shared by every caller and persisted. */
export function useTheme() {
  const [preference, setPreferenceState] = useState<ThemePreference>(readPreference);
  const [mode, setMode] = useState<Theme>(() => resolve(readPreference()));

  useEffect(() => {
    const sync = () => {
      const next = readPreference();
      setPreferenceState(next);
      setMode(resolve(next));
    };
    listeners.add(sync);
    return () => {
      listeners.delete(sync);
    };
  }, []);

  const setPreference = useCallback((next: ThemePreference) => writePreference(next), []);

  /** Two-state toggle used by the sidebar: flip the currently shown theme. */
  const cycleMode = useCallback(() => {
    writePreference(resolve(readPreference()) === "light" ? "dark" : "light");
  }, []);

  return { mode, preference, setPreference, cycleMode };
}
