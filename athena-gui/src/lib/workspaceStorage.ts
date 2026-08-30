/** Workspace (project directory) selection persistence. */

const RECENT_KEY = "athena.workspace.recent";
const MAX_RECENT = 8;

/** Add a root to the list, capping length; a known root keeps its position.
  *
  * 只有没见过的工作区才进列表头部。已经在列表里的**原地不动**：切到某个工作区
  * （包括点它下面的会话）不该把它拽到最前面，侧栏顺序要稳定。 */
export function addRecentRoot(root: string, existing: readonly string[]): string[] {
  const trimmed = root.trim();
  if (!trimmed) return [...existing];
  if (existing.includes(trimmed)) return [...existing];
  return [trimmed, ...existing].slice(0, MAX_RECENT);
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
