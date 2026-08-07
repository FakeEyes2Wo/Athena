import { useEffect, useState } from "react";
import { type PipelineEvent, subscribeToPipelineEvents } from "../lib/tauri-bridge";

const LIMIT = 200;

/** Subscribes to all pipeline events and returns the trailing window of the last 200. */
export function useEvents() {
  const [events, setEvents] = useState<PipelineEvent[]>([]);

  useEffect(() => {
    let mounted = true;
    let unlisteners: Array<() => void> = [];

    subscribeToPipelineEvents((event) => {
      if (!mounted) return;
      setEvents((prev) => [...prev.slice(-(LIMIT - 1)), event]);
    }).then((fns) => {
      if (!mounted) { fns.forEach((fn) => fn()); return; }
      unlisteners = fns;
    });

    return () => { mounted = false; unlisteners.forEach((fn) => fn()); };
  }, []);

  return events;
}
