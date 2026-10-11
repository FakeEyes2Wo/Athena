import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../lib/errors";
import { useResearchSignal } from "./useResearchSignal";

interface RemoteDataOptions {
  /** Re-fetch when the live research state changes: every change, phase only, or never. */
  follow?: "token" | "phase" | false;
}

/**
 * Fetch an RPC snapshot with loading/error state and manual refresh.
 *
 * Responses that arrive after a newer request started are dropped, so a slow
 * refresh can never overwrite fresher data.
 */
export function useRemoteData<T>(fetcher: () => Promise<T>, { follow = "token" }: RemoteDataOptions = {}) {
  const signal = useResearchSignal();
  const key = follow === "token" ? signal.token : follow === "phase" ? signal.phase : "";
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const requestRef = useRef(0);

  const refresh = useCallback(async () => {
    const id = ++requestRef.current;
    setLoading(true);
    try {
      const result = await fetcherRef.current();
      if (id !== requestRef.current) return;
      setData(result);
      setError(null);
    } catch (err) {
      if (id !== requestRef.current) return;
      setError(errorMessage(err));
    } finally {
      if (id === requestRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, key]);

  return { data, error, loading, refresh };
}
