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

function readStorage(key: string): unknown {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: unknown): void {
  try {
    if (value === undefined) {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, JSON.stringify(value));
    }
  } catch {
    // Storage may be unavailable (private mode / quota); memory remains authoritative.
  }
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
  const parsed = readStorage(RECENT_KEY);
  return Array.isArray(parsed)
    ? parsed
        .filter((item): item is string => typeof item === "string" && item.trim() !== "")
        .map((item) => item.trim())
        .slice(0, MAX_RECENT)
    : [];
}

/** Persist recent roots to localStorage (never throws). */
export function persistRecentRoots(roots: readonly string[]): void {
  writeStorage(RECENT_KEY, roots.slice(0, MAX_RECENT));
}

/** Read cached session summaries for one workspace (never throws). */
export function loadWorkspaceSessions(root: string): SessionSummary[] {
  return normalizeWorkspaceSessions(readStorage(workspaceSessionsKey(root)));
}

/** Persist authoritative session summaries for one workspace (never throws). */
export function persistWorkspaceSessions(
  root: string,
  sessions: readonly SessionSummary[],
): void {
  const normalized = normalizeWorkspaceSessions(sessions);
  writeStorage(
    workspaceSessionsKey(root),
    normalized.length > 0 ? normalized : undefined,
  );
}
