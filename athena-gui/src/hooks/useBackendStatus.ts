import { useEffect, useState } from "react";
import { isTauri } from "../lib/tauri-bridge";
import { wsBackend, type BackendStatus } from "../lib/ws-backend";

/**
 * Live connection status of the Python gateway.
 *
 * The Tauri shell owns its own bridge and reports failures per call, so it is
 * treated as always connected; the browser preview observes the WebSocket.
 */
export function useBackendStatus(): BackendStatus {
  const [status, setStatus] = useState<BackendStatus>(() =>
    isTauri ? "open" : wsBackend.currentStatus,
  );

  useEffect(() => {
    if (isTauri) return;
    const unsubscribe = wsBackend.onStatus(setStatus);
    // Make sure a connection attempt exists even before the first RPC.
    wsBackend.connect().catch(() => {
      // The socket schedules its own reconnects; status listeners report it.
    });
    return unsubscribe;
  }, []);

  return status;
}
