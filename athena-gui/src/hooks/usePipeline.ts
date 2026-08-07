import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  pauseSearch,
  resumeSearch,
  sendMessage,
  startSearch,
  stopSearch,
  subscribeToPipelineEvents,
  type PipelineEvent,
  type TaskPreview,
} from "../lib/tauri-bridge";
import {
  createEmptyPipelineViewModel,
  type ContextPanelKey,
  type PipelineViewModel,
} from "../types/ui";

const VALID_STATUSES = new Set(["idle", "running", "paused", "completed", "error"]);

/** Applies a single pipeline event to the current view model, returning a new copy. */
function applyPipelineEvent(current: PipelineViewModel, event: PipelineEvent): PipelineViewModel {
  const next: PipelineViewModel = { ...current, rightRail: { ...current.rightRail } };
  const { data } = event;

  if (typeof data.status === "string" && VALID_STATUSES.has(data.status)) {
    next.status = data.status as PipelineViewModel["status"];
  }

  if (typeof data.phase === "string" && data.phase.trim()) {
    next.phase = data.phase;
  }

  if (typeof data.remaining === "number") {
    next.rightRail.budgetRemaining = data.remaining;
  }

  if (typeof data.no_improve_streak === "number") {
    next.rightRail.noImproveStreak = data.no_improve_streak;
  }

  if (event.kind === "experiment/started") {
    next.phase = next.phase === "idle" ? "SEARCH" : next.phase;
    next.status = "running";
  }

  if (event.kind === "experiment/completed") {
    if (typeof data.experiment_id === "string") {
      next.rightRail.latestExperimentId = data.experiment_id;
    }
    if (typeof data.primary === "number") {
      const prev = next.rightRail.bestPrimary;
      next.rightRail.bestPrimary = prev == null ? data.primary : Math.max(prev, data.primary);
    }
  }

  return next;
}

/**
 * Central pipeline state hook.
 * Manages the view model, subscribes to backend events, and exposes all user actions
 * (send prompt, start/stop/pause/resume search, open/close panels).
 */
export function usePipeline() {
  const [viewModel, setViewModel] = useState<PipelineViewModel>(createEmptyPipelineViewModel);
  const counter = useRef(0);

  const nextId = useCallback((prefix: string) => {
    counter.current += 1;
    return `${prefix}-${counter.current}`;
  }, []);

  // Subscribe to backend pipeline events on mount.
  useEffect(() => {
    let mounted = true;
    let unlisteners: Array<() => void> = [];

    subscribeToPipelineEvents((event) => {
      if (!mounted) return;
      setViewModel((prev) => applyPipelineEvent(prev, event));
    }).then((fns) => {
      if (!mounted) { fns.forEach((fn) => fn()); return; }
      unlisteners = fns;
    });

    return () => { mounted = false; unlisteners.forEach((fn) => fn()); };
  }, []);

  // User actions.

  const sendPrompt = useCallback(async (msg: string) => {
    const content = msg.trim();
    if (!content) return;

    setViewModel((prev) => ({
      ...prev,
      messages: [...prev.messages, { id: nextId("user"), role: "user", kind: "text", content }],
    }));

    try {
      const preview = await sendMessage(content);
      setViewModel((prev) => ({
        ...prev,
        messages: [
          ...prev.messages,
          {
            id: nextId("preview"),
            role: "athena",
            kind: "intent-preview",
            content: `任务类型: ${preview.task_type} · 主指标: ${preview.primary_metric}`,
            preview,
          },
        ],
      }));
    } catch (err) {
      const text = err instanceof Error ? err.message : String(err);
      setViewModel((prev) => ({
        ...prev,
        status: "error",
        messages: [
          ...prev.messages,
          { id: nextId("error"), role: "athena", kind: "error", content: text },
        ],
      }));
      throw err;
    }
  }, [nextId]);

  const startRun = useCallback(async (preview: TaskPreview) => {
    setViewModel((prev) => ({
      ...prev,
      phase: "SEARCH",
      status: "running",
    }));
    try {
      await startSearch({ max_experiments: 10, ...preview });
    } catch (err) {
      setViewModel((prev) => ({
        ...prev,
        status: "error",
      }));
      throw err;
    }
  }, []);

  const openPanel = useCallback((panel: ContextPanelKey) => {
    setViewModel((prev) => ({
      ...prev,
      contextSurface: { isOpen: true, activePanel: panel },
    }));
  }, []);

  const closePanel = useCallback(() => {
    setViewModel((prev) => ({
      ...prev,
      contextSurface: { ...prev.contextSurface, isOpen: false },
    }));
  }, []);

  const pauseRun = useCallback(async () => {
    await pauseSearch();
    setViewModel((prev) => ({
      ...prev,
      status: "paused",
    }));
  }, []);

  const resumeRun = useCallback(async () => {
    await resumeSearch();
    setViewModel((prev) => ({
      ...prev,
      status: "running",
    }));
  }, []);

  const stopRun = useCallback(async () => {
    await stopSearch();
    setViewModel((prev) => ({
      ...prev,
      status: "completed",
    }));
  }, []);

  return useMemo(() => ({
    viewModel,
    sendPrompt,
    startRun,
    openPanel,
    closePanel,
    pauseRun,
    resumeRun,
    stopRun,
  }), [closePanel, openPanel, pauseRun, resumeRun, sendPrompt, startRun, stopRun, viewModel]);
}
