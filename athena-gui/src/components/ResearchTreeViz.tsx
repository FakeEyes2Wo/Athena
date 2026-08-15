import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errorMessage } from "../lib/errors";
import ReactFlow, {
  Background,
  BackgroundVariant,
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
import { layoutLineage } from "../lib/graphLayout";
import { EmptyState } from "./common/EmptyState";

const AUTO_SAVE_MS = 30_000;
const NODE_W = 220;
const NODE_H = 92;

/** Color palette mapping hypothesis status to visual style (semantic tokens, no literals). */
export const STATUS_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  SUPPORTED: { bg: "var(--status-supported-bg)", border: "var(--status-supported-border)", text: "var(--status-supported-text)" },
  REFUTED: { bg: "var(--status-refuted-bg)", border: "var(--status-refuted-border)", text: "var(--status-refuted-text)" },
  REJECTED: { bg: "var(--status-rejected-bg)", border: "var(--status-rejected-border)", text: "var(--status-rejected-text)" },
  PROPOSED: { bg: "var(--status-proposed-bg)", border: "var(--status-proposed-border)", text: "var(--status-proposed-text)" },
};

export function statusColor(status: string) {
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
    : "无指标";

  return (
    <div className="flow-node" style={{ borderColor: colors.border }}>
      <Handle type="target" position={Position.Top} />
      <div className="flow-node__title">
        {data.hypothesis.statement.slice(0, 48)}
        {data.hypothesis.statement.length > 48 ? "…" : ""}
      </div>
      <div className="flow-node__meta">
        Primary: <strong>{result}</strong>
      </div>
      <div
        className="flow-node__badge"
        style={{ background: colors.bg, borderColor: colors.border, color: colors.text }}
      >
        {data.experiment.status}
      </div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { researchNode: ResearchNode };

/** Converts a ResearchTreeData model into React Flow nodes/edges laid out by d3-dag. */
function buildGraph(tree: ResearchTreeData): { nodes: Node[]; edges: Edge[] } {
  const ids = Object.keys(tree.experiments);
  const lineageEdges: { source: string; target: string }[] = [];

  for (const [id, experiment] of Object.entries(tree.experiments)) {
    const hypothesis = tree.hypotheses[experiment.hypothesis_id];
    if (!hypothesis) {
      throw new Error(`Experiment ${id} references missing hypothesis ${experiment.hypothesis_id}`);
    }
    if (experiment.parent_id && tree.experiments[experiment.parent_id]) {
      lineageEdges.push({ source: experiment.parent_id, target: id });
    }
  }

  const positions = layoutLineage(ids, lineageEdges, { nodeWidth: NODE_W, nodeHeight: NODE_H });

  const nodes: Node[] = Object.entries(tree.experiments).map(([id, experiment]) => {
    const hypothesis = tree.hypotheses[experiment.hypothesis_id];
    const pos = positions.get(id) ?? { x: 0, y: 0 };
    return {
      id,
      type: "researchNode",
      position: pos,
      data: { id, experiment, hypothesis },
    };
  });

  const edges: Edge[] = lineageEdges.map((edge) => ({
    id: `${edge.source}->${edge.target}`,
    source: edge.source,
    target: edge.target,
    markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: "var(--text-tertiary)" },
    style: { stroke: "var(--text-tertiary)", strokeWidth: 1.75 },
  }));

  return { nodes, edges };
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
    <div className="panel-toolbar">
      <h3>研究树 ({nodeCount} 个实验)</h3>
      <div className="panel-toolbar__actions">
        <button className="btn btn--subtle btn--sm" onClick={onRefresh}>刷新</button>
        <button className="btn btn--subtle btn--sm" onClick={onSave} disabled={saving}>
          {saving ? "保存中…" : "保存"}
        </button>
        <label className="switch">
          <input type="checkbox" checked={autoSave} onChange={onToggleAutoSave} />
          <span className="switch__track" />
          自动保存
        </label>
      </div>
    </div>
  );
}

/**
 * Interactive research tree visualization.
 * Loads experiment data from the backend, renders a React Flow graph laid out
 * by d3-dag, and supports manual drag, save/refresh, and auto-save.
 */
export default function ResearchTreeViz() {
  const [tree, setTree] = useState<ResearchTreeData | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [autoSave, setAutoSave] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await treeGet();
      setTree(result.tree);
      setError(null);
    } catch (err) {
      // Keep the last rendered tree, but surface the failure when there is nothing to show.
      setError(errorMessage(err));
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
        {error && <div className="card card--error" style={{ marginBottom: 10 }}>{error}</div>}
        <EmptyState icon="tree" message="暂无实验数据。启动搜索后研究树将自动构建。" />
      </div>
    );
  }

  return (
    <div className="detail-panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <ControlsBar
        saving={saving} autoSave={autoSave} nodeCount={nodeCount}
        onRefresh={refresh} onSave={save} onToggleAutoSave={toggleAutoSave}
      />
      <div className="graph-container" role="region" aria-label="研究树可视化">
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
          <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="var(--border-strong)" />
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
