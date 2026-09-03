/** Workspace (project directory) selection persistence. */

const RECENT_KEY = "athena.workspace.recent";
const MAX_RECENT = 8;
const WORKSPACE_SESSIONS_PREFIX = "athena.workspace.sessions:";
const MAX_WORKSPACE_SESSIONS = 200;

export interface SessionSummary {
  id: string;
  title: string;
}

function workspaceSessionsKey(root: string): string {
  return `${WORKSPACE_SESSIONS_PREFIX}${root}`;
}

function normalizeWorkspaceSessions(value: unknown): SessionSummary[] {
  if (!Array.isArray(value)) return [];

  const seen = new Set<string>();
  const normalized: SessionSummary[] = [];
  for (const item of value) {
    if (typeof item !== "object" || item === null) continue;
    const { id, title } = item as Partial<SessionSummary>;
    if (typeof id !== "string" || typeof title !== "string") continue;
    const normalizedId = id.trim();
    const normalizedTitle = title.trim();
    if (!normalizedId || !normalizedTitle || seen.has(normalizedId)) continue;
    seen.add(normalizedId);
    normalized.push({ id: normalizedId, title: normalizedTitle });
    if (normalized.length === MAX_WORKSPACE_SESSIONS) break;
  }
  return normalized;
}

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

/** Read cached session summaries for one workspace (never throws). */
export function loadWorkspaceSessions(root: string): SessionSummary[] {
  try {
    const raw = localStorage.getItem(workspaceSessionsKey(root));
    if (!raw) return [];
    return normalizeWorkspaceSessions(JSON.parse(raw));
  } catch {
    return [];
  }
}

/** Persist authoritative session summaries for one workspace (never throws). */
export function persistWorkspaceSessions(
  root: string,
  sessions: readonly SessionSummary[],
): void {
  try {
    const normalized = normalizeWorkspaceSessions(sessions);
    if (normalized.length === 0) {
      localStorage.removeItem(workspaceSessionsKey(root));
      return;
    }
    localStorage.setItem(workspaceSessionsKey(root), JSON.stringify(normalized));
  } catch {
    // Storage may be unavailable; authoritative in-memory state remains usable.
  }
}
