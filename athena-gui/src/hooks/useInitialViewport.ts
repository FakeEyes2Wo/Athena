import { useCallback, useEffect, useRef, useState } from "react";
import type { Node, ReactFlowInstance } from "reactflow";
import { initialViewport } from "../lib/graphLayout";

/**
 * Places the ReactFlow camera once, when the graph first has nodes: fit if
 * legible, otherwise a readable zoom anchored on the root. Later refreshes keep
 * whatever the user has panned or zoomed to.
 */
export function useInitialViewport(nodes: Node[], size: { nodeWidth: number; nodeHeight: number }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [flow, setFlow] = useState<ReactFlowInstance | null>(null);
  const placedRef = useRef(false);
  const { nodeWidth, nodeHeight } = size;

  useEffect(() => {
    const el = containerRef.current;
    if (!flow || !el || placedRef.current || nodes.length === 0) return;
    if (el.clientWidth === 0 || el.clientHeight === 0) return;
    placedRef.current = true;
    flow.setViewport(
      initialViewport(
        nodes.map((node) => node.position),
        { width: el.clientWidth, height: el.clientHeight },
        { nodeWidth, nodeHeight },
      ),
    );
  }, [flow, nodes, nodeWidth, nodeHeight]);

  const onInit = useCallback((instance: ReactFlowInstance) => setFlow(instance), []);
  return { containerRef, onInit };
}
