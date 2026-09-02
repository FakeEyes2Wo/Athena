/** Workspace (project directory) selection persistence. */

const RECENT_KEY = "athena.workspace.recent";
const MAX_RECENT = 8;

/** Keep known roots in place; prepend a new root and cap the list length. */
export function addRecentRoot(root: string, existing: readonly string[]): string[] {
  const trimmed = root.trim();
  if (!trimmed) return [...existing];
  if (existing.includes(trimmed)) return [...existing].slice(0, MAX_RECENT);
  const next = [trimmed, ...existing];
  return next.slice(0, MAX_RECENT);
}

/** Read recent roots from localStorage (never throws; returns [] on parse errors). */
export function loadRecentRoots(): string[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is string => typeof item === "string" && item.trim() !== "")
      .map((item) => item.trim())
      .slice(0, MAX_RECENT);
  } catch {
    return [];
  }
}

/** Persist recent roots to localStorage (never throws). */
export function persistRecentRoots(roots: readonly string[]): void {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(roots.slice(0, MAX_RECENT)));
  } catch {
    // Storage may be unavailable (private mode / quota); selection still works in-memory.
  }
}
