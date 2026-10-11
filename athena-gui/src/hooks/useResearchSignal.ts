import { createContext, useContext } from "react";

/**
 * A cheap fingerprint of the live research state (phase, attempts, SOTA, ...).
 *
 * Panels that fetch snapshots over RPC re-fetch whenever ``token`` changes, so
 * open views follow a running search without polling. Outside a provider the
 * values are constant and panels fetch once on mount.
 */
export interface ResearchSignal {
  token: string;
  phase: string;
  /** A run is actively executing (manual overrides should wait). */
  running: boolean;
}

export const ResearchSignalContext = createContext<ResearchSignal>({ token: "", phase: "", running: false });

export function useResearchSignal(): ResearchSignal {
  return useContext(ResearchSignalContext);
}
