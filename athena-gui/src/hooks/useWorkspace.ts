import { useCallback, useEffect, useMemo, useState } from "react";
import { errorMessage } from "../lib/errors";
import { setProjectRoot, settingsGet } from "../lib/tauri-bridge";
import {
  addRecentRoot,
  loadRecentRoots,
  persistRecentRoots,
} from "../lib/workspaceStorage";

/** Namespaced return shape for the workspace-selection hook. */
export interface WorkspaceState {
  /** Active project root reported by the backend (null until first fetch). */
  currentRoot: string | null;
  /** Recently used project roots, most-recent first. */
  recentRoots: string[];
  /** True once the initial settings snapshot has loaded. */
  ready: boolean;
  /** Whether the selection screen/modal is visible (always open on startup). */
  pickerOpen: boolean;
  /** True while a project switch is in flight. */
  switching: boolean;
  /** Last switch/load error message, if any. */
  error: string | null;
}

export interface WorkspaceActions {
  switchTo(path: string): Promise<void>;
  openPicker(): void;
  closePicker(): void;
}

/**
 * Manages the active project directory (workspace) for the GUI.
 *
 * On startup it fetches the backend's current root, then opens the picker so the
 * user can choose or continue. Switching delegates to the backend ``set_project_root``
 * RPC and records the path in a localStorage recent list.
 */
export function useWorkspace(): WorkspaceState & WorkspaceActions {
  const [currentRoot, setCurrentRoot] = useState<string | null>(null);
  const [recentRoots, setRecentRoots] = useState<string[]>([]);
  const [ready, setReady] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setRecentRoots(loadRecentRoots());
    settingsGet()
      .then((settings) => {
        if (!mounted) return;
        const root = settings.project_root || null;
        setCurrentRoot(root);
        // 后端已恢复上次项目目录时直接进入主界面，不再弹选择页。
        if (root) setPickerOpen(false);
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

  const switchTo = useCallback(async (path: string) => {
    const trimmed = path.trim();
    if (!trimmed) return;
    setSwitching(true);
    setError(null);
    try {
      const settings = await setProjectRoot(trimmed);
      setCurrentRoot(settings.project_root || trimmed);
      setRecentRoots((prev) => {
        const next = addRecentRoot(settings.project_root || trimmed, prev);
        persistRecentRoots(next);
        return next;
      });
      setPickerOpen(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSwitching(false);
    }
  }, []);

  const openPicker = useCallback(() => setPickerOpen(true), []);
  const closePicker = useCallback(() => setPickerOpen(false), []);

  return useMemo(
    () => ({
      currentRoot,
      recentRoots,
      ready,
      pickerOpen,
      switching,
      error,
      switchTo,
      openPicker,
      closePicker,
    }),
    [currentRoot, recentRoots, ready, pickerOpen, switching, error, switchTo, openPicker, closePicker],
  );
}
