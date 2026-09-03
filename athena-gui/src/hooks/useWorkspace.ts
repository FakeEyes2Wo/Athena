import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errorMessage } from "../lib/errors";
import { setProjectRoot, settingsGet } from "../lib/tauri-bridge";
import { selectWorkspaceDirectory } from "../lib/workspaceDialog";
import {
  addRecentRoot,
  loadRecentRoots,
  persistRecentRoots,
} from "../lib/workspaceStorage";

/** Namespaced return shape for the workspace-selection hook. */
export interface WorkspaceState {
  /** Active project root reported by the backend (null until first fetch). */
  currentRoot: string | null;
  /** Remembered project roots in stable display order. */
  recentRoots: string[];
  /** Session requested while crossing into another workspace. */
  requestedSessionId: string | null;
  /** True once the initial settings snapshot has loaded. */
  ready: boolean;
  /** Whether the selection screen/modal is visible. */
  pickerOpen: boolean;
  /** True while a project switch is in flight. */
  switching: boolean;
  /** True while the native directory picker is open. */
  browsing: boolean;
  /** Last switch/load error message, if any. */
  error: string | null;
}

export interface WorkspaceActions {
  switchTo(path: string, sessionId?: string): Promise<void>;
  browse(): Promise<void>;
  openPicker(): void;
  closePicker(): void;
}

interface WorkspaceIntent {
  path: string;
  sessionId: string | null;
}

/**
 * Manages the active project directory (workspace) for the GUI.
 *
 * On startup it fetches the backend's current root and reuses it only when that
 * root was remembered locally. Switching delegates to the backend
 * ``set_project_root`` RPC and records new paths in a stable localStorage list.
 */
export function useWorkspace(): WorkspaceState & WorkspaceActions {
  const [currentRoot, setCurrentRoot] = useState<string | null>(null);
  const [recentRoots, setRecentRoots] = useState<string[]>([]);
  const [requestedSessionId, setRequestedSessionId] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(true);
  const [switching, setSwitching] = useState(false);
  const mountedRef = useRef(true);
  const activeSwitchRef = useRef<Promise<void> | null>(null);
  const queuedIntentRef = useRef<WorkspaceIntent | null>(null);
  const [browsing, setBrowsing] = useState(false);
  const browsingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    mountedRef.current = true;
    const rememberedRoots = loadRecentRoots();
    setRecentRoots(rememberedRoots);
    settingsGet()
      .then((settings) => {
        if (!mounted || !mountedRef.current) return;
        const root = settings.project_root || null;
        setCurrentRoot(root);
        // 后端已恢复上次项目目录时直接进入主界面，不再弹选择页。
        setPickerOpen(!root || !rememberedRoots.includes(root));
        setReady(true);
      })
      .catch((err: unknown) => {
        if (!mounted || !mountedRef.current) return;
        setError(errorMessage(err));
        setReady(true);
      });
    return () => {
      mounted = false;
      mountedRef.current = false;
      queuedIntentRef.current = null;
    };
  }, []);

  const switchTo = useCallback((path: string, sessionId?: string): Promise<void> => {
    if (!mountedRef.current) return Promise.resolve();
    const trimmed = path.trim();
    if (!trimmed) return Promise.resolve();
    queuedIntentRef.current = { path: trimmed, sessionId: sessionId ?? null };
    setSwitching(true);
    setError(null);

    if (activeSwitchRef.current) return activeSwitchRef.current;

    const processSwitches = async () => {
      try {
        while (queuedIntentRef.current) {
          const intent = queuedIntentRef.current;
          queuedIntentRef.current = null;
          try {
            const settings = await setProjectRoot(intent.path);
            if (!mountedRef.current) return;
            if (queuedIntentRef.current) continue;
            const resolvedRoot = settings.project_root || intent.path;
            setCurrentRoot(resolvedRoot);
            setRequestedSessionId(intent.sessionId);
            setRecentRoots((prev) => {
              const next = addRecentRoot(resolvedRoot, prev);
              persistRecentRoots(next);
              return next;
            });
            setPickerOpen(false);
          } catch (err) {
            if (!mountedRef.current) return;
            if (!queuedIntentRef.current) setError(errorMessage(err));
          }
        }
      } finally {
        activeSwitchRef.current = null;
        if (mountedRef.current) setSwitching(false);
      }
    };

    const activeSwitch = processSwitches();
    activeSwitchRef.current = activeSwitch;
    return activeSwitch;
  }, []);

  const browse = useCallback(async () => {
    if (!mountedRef.current || browsingRef.current) return;
    browsingRef.current = true;
    setBrowsing(true);
    setError(null);
    try {
      const selected = await selectWorkspaceDirectory(currentRoot);
      if (!mountedRef.current) return;
      if (selected) await switchTo(selected);
    } catch (err) {
      if (mountedRef.current) setError(errorMessage(err));
    } finally {
      browsingRef.current = false;
      if (mountedRef.current) setBrowsing(false);
    }
  }, [currentRoot, switchTo]);

  const openPicker = useCallback(() => setPickerOpen(true), []);
  const closePicker = useCallback(() => setPickerOpen(false), []);

  return useMemo(
    () => ({
      currentRoot,
      recentRoots,
      requestedSessionId,
      ready,
      pickerOpen,
      switching,
      browsing,
      error,
      switchTo,
      browse,
      openPicker,
      closePicker,
    }),
    [currentRoot, recentRoots, requestedSessionId, ready, pickerOpen, switching, browsing, error, switchTo, browse, openPicker, closePicker],
  );
}
