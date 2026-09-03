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
  const switchEpochRef = useRef(0);
  const [browsing, setBrowsing] = useState(false);
  const browsingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    const rememberedRoots = loadRecentRoots();
    setRecentRoots(rememberedRoots);
    settingsGet()
      .then((settings) => {
        if (!mounted) return;
        const root = settings.project_root || null;
        setCurrentRoot(root);
        // 后端已恢复上次项目目录时直接进入主界面，不再弹选择页。
        setPickerOpen(!root || !rememberedRoots.includes(root));
        setReady(true);
      })
      .catch((err: unknown) => {
        if (!mounted) return;
        setError(errorMessage(err));
        setReady(true);
      });
    return () => {
      mounted = false;
    };
  }, []);

  const switchTo = useCallback(async (path: string, sessionId?: string) => {
    const trimmed = path.trim();
    if (!trimmed) return;
    const switchEpoch = ++switchEpochRef.current;
    setSwitching(true);
    setError(null);
    try {
      const settings = await setProjectRoot(trimmed);
      if (switchEpoch !== switchEpochRef.current) return;
      setCurrentRoot(settings.project_root || trimmed);
      setRequestedSessionId(sessionId ?? null);
      setRecentRoots((prev) => {
        const next = addRecentRoot(settings.project_root || trimmed, prev);
        persistRecentRoots(next);
        return next;
      });
      setPickerOpen(false);
    } catch (err) {
      if (switchEpoch === switchEpochRef.current) {
        setError(errorMessage(err));
      }
    } finally {
      if (switchEpoch === switchEpochRef.current) {
        setSwitching(false);
      }
    }
  }, []);

  const browse = useCallback(async () => {
    if (browsingRef.current || switching) return;
    browsingRef.current = true;
    setBrowsing(true);
    setError(null);
    try {
      const selected = await selectWorkspaceDirectory(currentRoot);
      if (selected) await switchTo(selected);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      browsingRef.current = false;
      setBrowsing(false);
    }
  }, [currentRoot, switching, switchTo]);

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
