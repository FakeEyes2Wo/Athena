import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  type Node,
  type Edge,
  type NodeChange,
  useNodesState,
  useEdgesState,
  MarkerType,
} from "reactflow";
import "reactflow/dist/style.css";
import {
  treeGet,
  treeSave,
  type HypothesisData,
  type ResearchExperimentData,
  type ResearchTreeData,
} from "../lib/tauri-bridge";

const AUTO_SAVE_MS = 30_000;

/** Color palette mapping hypothesis status to visual style. */
const STATUS_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  SUPPORTED: { bg: "#dcfce7", border: "#22c55e", text: "#166534" },
  REFUTED: { bg: "#fee2e2", border: "#ef4444", text: "#991b1b" },
  REJECTED: { bg: "#f3f4f6", border: "#9ca3af", text: "#374151" },
  PROPOSED: { bg: "#eff6ff", border: "#3b82f6", text: "#1e40af" },
};

function statusColor(status: string) {
  return STATUS_COLORS[status] ?? STATUS_COLORS.PROPOSED;
}

/** Custom React Flow node rendering a research experiment with hypothesis status. */
interface ResearchNodeData {
  id: string;
  experiment: ResearchExperimentData;
  hypothesis: HypothesisData;
}

function ResearchNode({ data }: { data: ResearchNodeData }) {
  const colors = statusColor(data.hypothesis.status);
  const result = data.experiment.eval
    ? data.experiment.eval.primary.toFixed(4)
    : "No metric";

  return (
    <div
      style={{
        background: colors.bg,
        border: `2px solid ${colors.border}`,
        borderRadius: 12,
        padding: "10px 14px",
        minWidth: 200,
        maxWidth: 280,
        fontSize: 13,
      }}
    >
      <Handle type="target" position={Position.Top} />
      <div style={{ fontWeight: 600, marginBottom: 4, color: colors.text }}>
        {data.hypothesis.statement.slice(0, 48)}
        {data.hypothesis.statement.length > 48 ? "..." : ""}
      </div>
      <div style={{ fontSize: 12, color: "#6b7280" }}>
        Primary: <strong>{result}</strong>
      </div>
      <div style={{
        fontSize: 11,
        marginTop: 4,
        color: colors.text,
        background: "rgba(255,255,255,0.6)",
        borderRadius: 6,
        padding: "1px 8px",
        display: "inline-block",
      }}>
        {data.experiment.status}
      </div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { researchNode: ResearchNode };

/** Converts a ResearchTreeData model into React Flow nodes and edges. */
function buildGraph(tree: ResearchTreeData): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  const childrenMap = new Map<string, string[]>();
  const parentMap = new Map<string, string | null>();

  for (const [id, experiment] of Object.entries(tree.experiments)) {
    const hypothesis = tree.hypotheses[experiment.hypothesis_id];
    if (!hypothesis) {
      throw new Error(
        `Experiment ${id} references missing hypothesis ${experiment.hypothesis_id}`,
      );
    }
    nodes.push({
      id,
      type: "researchNode",
      position: { x: 0, y: 0 },
      data: { id, experiment, hypothesis },
    });

    parentMap.set(id, experiment.parent_id);

    if (experiment.parent_id && tree.experiments[experiment.parent_id]) {
      edges.push({
        id: `${experiment.parent_id}->${id}`,
        source: experiment.parent_id,
        target: id,
        markerEnd: { type: MarkerType.ArrowClosed, width: 12, height: 12, color: "#94a3b8" },
        style: { stroke: "#94a3b8", strokeWidth: 2 },
      });
      const kids = childrenMap.get(experiment.parent_id) ?? [];
      kids.push(id);
      childrenMap.set(experiment.parent_id, kids);
    }
  }

  return { nodes: layoutTreeLayers(nodes, edges, childrenMap, parentMap), edges };
}

/** Positions tree nodes in layers using a BFS walk from root nodes. */
function layoutTreeLayers(
  nodes: Node[],
  _edges: Edge[],
  childrenMap: Map<string, string[]>,
  parentMap: Map<string, string | null>,
): Node[] {
  const roots = nodes.filter((n) => parentMap.get(n.id) === null);
  const layers: string[][] = [];
  const visited = new Set<string>();

  function walk(ids: string[], depth: number) {
    if (ids.length === 0) return;
    if (!layers[depth]) layers[depth] = [];
    const next: string[] = [];
    for (const id of ids) {
      if (visited.has(id)) continue;
      visited.add(id);
      layers[depth].push(id);
      for (const kid of childrenMap.get(id) ?? []) {
        if (!visited.has(kid)) next.push(kid);
      }
    }
    walk(next, depth + 1);
  }

  walk(roots.map((r) => r.id), 0);

  const Y_GAP = 150;
  const X_GAP = 280;

  return nodes.map((n) => {
    let layerIdx = -1;
    let posInLayer = 0;
    for (let i = 0; i < layers.length; i++) {
      const idx = layers[i].indexOf(n.id);
      if (idx !== -1) { layerIdx = i; posInLayer = idx; break; }
    }
    if (layerIdx === -1) { layerIdx = 0; }

    const layerLen = layers[layerIdx]?.length ?? 1;
    const xOffset = -(layerLen - 1) * X_GAP / 2;

    return { ...n, position: { x: xOffset + posInLayer * X_GAP, y: layerIdx * Y_GAP } };
  });
}

/** Toolbar with save, refresh, and auto-save toggle for the research tree. */
function ControlsBar({
  saving,
  autoSave,
  nodeCount,
  onRefresh,
  onSave,
  onToggleAutoSave,
}: {
  saving: boolean;
  autoSave: boolean;
  nodeCount: number;
  onRefresh: () => void;
  onSave: () => void;
  onToggleAutoSave: () => void;
}) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
      <h3 style={{ margin: 0 }}>研究树 ({nodeCount} 个实验)</h3>
      <div style={{ display: "flex", gap: 8 }}>
        <button className="card__action" onClick={onRefresh}>刷新</button>
        <button className="card__action" onClick={onSave} disabled={saving}>
          {saving ? "保存中…" : "保存"}
        </button>
        <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
          <input type="checkbox" checked={autoSave} onChange={onToggleAutoSave} />
          自动保存
        </label>
      </div>
    </div>
  );
}

/**
 * Interactive research tree visualization.
 * Loads experiment data from the backend, renders a React Flow graph,
 * and supports manual drag, save/refresh, and auto-save.
 */
export default function ResearchTreeViz() {
  const [tree, setTree] = useState<ResearchTreeData | null>(null);
  const [saving, setSaving] = useState(false);
  const [autoSave, setAutoSave] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await treeGet();
      setTree(result.tree);
    } catch {
      // Keep the last rendered tree while the backend is unavailable.
    }
  }, []);

  const save = useCallback(async () => {
    setSaving(true);
    try {
      await treeSave();
      await refresh();
    } finally {
      setSaving(false);
    }
  }, [refresh]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (autoSave) {
      timerRef.current = setInterval(() => void save(), AUTO_SAVE_MS);
    } else if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }

    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [autoSave, save]);

  const toggleAutoSave = useCallback(() => setAutoSave((value) => !value), []);
  const nodeCount = tree ? Object.keys(tree.experiments).length : 0;

  const graph = useMemo(
    () => (tree && nodeCount > 0 ? buildGraph(tree) : { nodes: [] as Node[], edges: [] as Edge[] }),
    [tree, nodeCount],
  );

  const positionedNodeIdsRef = useRef(new Set<string>());
  const [nodes, setNodes, applyNodesChange] = useNodesState(graph.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(graph.edges);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    for (const change of changes) {
      if (change.type === "position" && change.position) {
        positionedNodeIdsRef.current.add(change.id);
      }
    }
    applyNodesChange(changes);
  }, [applyNodesChange]);

  useEffect(() => {
    setNodes((currentNodes) => {
      const currentNodesById = new Map(currentNodes.map((node) => [node.id, node]));
      const refreshedNodeIds = new Set(graph.nodes.map((node) => node.id));

      for (const id of positionedNodeIdsRef.current) {
        if (!refreshedNodeIds.has(id)) {
          positionedNodeIdsRef.current.delete(id);
        }
      }

      return graph.nodes.map((node) => ({
        ...node,
        position: positionedNodeIdsRef.current.has(node.id)
          ? currentNodesById.get(node.id)?.position ?? node.position
          : node.position,
      }));
    });
    setEdges(graph.edges);
  }, [graph.edges, graph.nodes, setEdges, setNodes]);

  if (!tree || nodeCount === 0) {
    return (
      <div className="detail-panel">
        <ControlsBar
          saving={saving} autoSave={autoSave} nodeCount={0}
          onRefresh={refresh} onSave={save} onToggleAutoSave={toggleAutoSave}
        />
        <p style={{ color: "#6b7280", marginTop: 12 }}>
          暂无实验数据。启动搜索后研究树将自动构建。
        </p>
      </div>
    );
  }

  return (
    <div className="detail-panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <ControlsBar
        saving={saving} autoSave={autoSave} nodeCount={nodeCount}
        onRefresh={refresh} onSave={save} onToggleAutoSave={toggleAutoSave}
      />
      <div style={{ flex: 1, minHeight: 360, border: "1px solid var(--border)", borderRadius: 14, overflow: "hidden" }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.3 }}
          minZoom={0.3}
          maxZoom={2}
        >
          <Background color="#e5e7eb" gap={24} />
          <Controls />
          <MiniMap
            nodeColor={(n) => {
              const d = n.data as ResearchNodeData;
              return statusColor(d.hypothesis.status).border;
            }}
            style={{ borderRadius: 8 }}
          />
        </ReactFlow>
      </div>
    </div>
  );
}
