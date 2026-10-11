import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
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
import { formatMetric } from "../lib/labels";
import { useResearchSignal } from "../hooks/useResearchSignal";
import { useInitialViewport } from "../hooks/useInitialViewport";
import { EmptyState } from "./common/EmptyState";
import { Icon } from "./common/Icon";
import { EXPERIMENT_STATUS, HYPOTHESIS_STATUS, StatusBadge } from "./common/StatusBadge";

const AUTO_SAVE_MS = 30_000;
const NODE_W = 220;
const NODE_H = 92;

/** Color palette mapping hypothesis status to visual style (semantic tokens, no literals). */
export const STATUS_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  SUPPORTED: { bg: "var(--status-supported-bg)", border: "var(--status-supported-border)", text: "var(--status-supported-text)" },
  REFUTED: { bg: "var(--status-refuted-bg)", border: "var(--status-refuted-border)", text: "var(--status-refuted-text)" },
  INCONCLUSIVE: {
    bg: "var(--status-inconclusive-bg)",
    border: "var(--status-inconclusive-border)",
    text: "var(--status-inconclusive-text)",
  },
  REJECTED: { bg: "var(--status-rejected-bg)", border: "var(--status-rejected-border)", text: "var(--status-rejected-text)" },
  PROPOSED: { bg: "var(--status-proposed-bg)", border: "var(--status-proposed-border)", text: "var(--status-proposed-text)" },
};

export function statusColor(status: string) {
  return STATUS_COLORS[status] ?? STATUS_COLORS.PROPOSED;
}

/** Status legend shared by both graph views. */
export function StatusLegend({ children }: { children?: ReactNode }) {
  return (
    <div className="legend">
      <span className="legend__title">假设状态</span>
      <span className="legend__swatches">
        {Object.entries(STATUS_COLORS).map(([status, colors]) => (
          <span key={status} className="badge" style={{ background: colors.bg, color: colors.text }}>
            {HYPOTHESIS_STATUS[status]?.label ?? status}
          </span>
        ))}
      </span>
      {children}
    </div>
  );
}

/** Custom React Flow node rendering a research experiment with hypothesis status. */
interface ResearchNodeData {
  id: string;
  experiment: ResearchExperimentData;
  hypothesis: HypothesisData;
  sota: boolean;
}

function ResearchNode({ data }: { data: ResearchNodeData }) {
  const colors = statusColor(data.hypothesis.status);
  const result = data.experiment.eval ? data.experiment.eval.primary.toFixed(4) : "无指标";

  return (
    <div className={`flow-node${data.sota ? " flow-node--sota" : ""}`} style={{ borderLeftColor: colors.border }}>
      <Handle type="target" position={Position.Top} />
      <div className="flow-node__title" title={data.hypothesis.statement}>
        {data.hypothesis.statement}
      </div>
      <div className="flow-node__meta">
        <span>
          {data.sota && <span style={{ color: "var(--gold)" }}>★ </span>}
          <strong>{result}</strong>
        </span>
        <span className="flow-node__badge" style={{ background: colors.bg, color: colors.text }}>
          {EXPERIMENT_STATUS[data.experiment.status]?.label ?? data.experiment.status}
        </span>
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
      data: { id, experiment, hypothesis, sota: tree.sota_id === id },
    };
  });

  const edges: Edge[] = lineageEdges.map((edge) => ({
    id: `${edge.source}->${edge.target}`,
    source: edge.source,
    target: edge.target,
    markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: "var(--text-tertiary)" },
    style: { stroke: "var(--text-tertiary)", strokeWidth: 1.5 },
  }));

  return { nodes, edges };
}

/** Toolbar with stats, refresh, save and auto-save toggle for the research tree. */
function ControlsBar({
  saving,
  autoSave,
  nodeCount,
  sotaId,
  onRefresh,
  onSave,
  onToggleAutoSave,
}: {
  saving: boolean;
  autoSave: boolean;
  nodeCount: number;
  sotaId: string | null;
  onRefresh: () => void;
  onSave: () => void;
  onToggleAutoSave: () => void;
}) {
  return (
    <div className="panel-toolbar">
      <h3>
        {nodeCount} 个实验
        {sotaId && (
          <span className="badge badge--gold" title={sotaId}>
            <Icon name="star" size={11} /> SOTA {sotaId}
          </span>
        )}
      </h3>
      <div className="panel-toolbar__actions">
        <button type="button" className="btn btn--ghost btn--sm" onClick={onRefresh}>
          <Icon name="refresh" size={13} /> 刷新
        </button>
        <button type="button" className="btn btn--ghost btn--sm" onClick={onSave} disabled={saving} title="将内存中的研究树写入磁盘">
          <Icon name="download" size={13} /> {saving ? "保存中…" : "保存"}
        </button>
        <label className="switch" title="每 30 秒自动保存研究树">
          <input type="checkbox" checked={autoSave} onChange={onToggleAutoSave} />
          <span className="switch__track" />
          自动保存
        </label>
      </div>
    </div>
  );
}

function NodeDetail({ data, onClose }: { data: ResearchNodeData; onClose(): void }) {
  return (
    <div className="graph-detail" role="region" aria-label="节点详情">
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, fontWeight: 600, flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>
          {data.id}
        </span>
        <button type="button" className="icon-btn icon-btn--sm" onClick={onClose} aria-label="关闭节点详情">
          <Icon name="close" size={14} />
        </button>
      </div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        <StatusBadge status={data.experiment.status} />
        <StatusBadge status={data.hypothesis.status} kind="hypothesis" />
        {data.sota && <span className="badge badge--gold">★ SOTA</span>}
      </div>
      <p style={{ margin: 0, lineHeight: 1.6 }}>{data.hypothesis.statement}</p>
      <div className="kv">
        <span className="kv__label">主指标</span>
        <span className="kv__value kv__value--mono">{formatMetric(data.experiment.eval?.primary)}</span>
      </div>
      {data.experiment.plan.change && (
        <div className="kv">
          <span className="kv__label">变更</span>
          <span className="kv__value">{data.experiment.plan.change}</span>
        </div>
      )}
      {data.experiment.error && (
        <div className="kv">
          <span className="kv__label">错误</span>
          <span className="kv__value" style={{ color: "var(--danger)" }}>{data.experiment.error}</span>
        </div>
      )}
    </div>
  );
}

/**
 * Interactive research tree visualization.
 * Loads experiment data from the backend, renders a React Flow graph laid out
 * by d3-dag, and supports manual drag, save/refresh, and auto-save.
 */
export default function ResearchTreeViz() {
  const { token } = useResearchSignal();
  const [tree, setTree] = useState<ResearchTreeData | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [autoSave, setAutoSave] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
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
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }, [refresh]);

  useEffect(() => {
    void refresh();
  }, [refresh, token]);

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

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      for (const change of changes) {
        if (change.type === "position" && change.position) {
          positionedNodeIdsRef.current.add(change.id);
        }
      }
      applyNodesChange(changes);
    },
    [applyNodesChange],
  );

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

  const viewport = useInitialViewport(graph.nodes, { nodeWidth: NODE_W, nodeHeight: NODE_H });

  const selectedData = selected ? (graph.nodes.find((node) => node.id === selected)?.data as ResearchNodeData | undefined) : undefined;

  if (!tree || nodeCount === 0) {
    return (
      <div className="detail-panel">
        <ControlsBar
          saving={saving}
          autoSave={autoSave}
          nodeCount={0}
          sotaId={null}
          onRefresh={refresh}
          onSave={save}
          onToggleAutoSave={toggleAutoSave}
        />
        {error && (
          <div className="callout callout--error" role="alert">
            <Icon name="alert" size={15} />
            <div className="callout__body">{error}</div>
          </div>
        )}
        <EmptyState
          icon="tree"
          title="研究树还是空的"
          message="研究开始后，基线实验会成为根节点，每次搜索尝试都作为子节点挂在它的父实验下。"
        />
      </div>
    );
  }

  return (
    <div className="detail-panel detail-panel--fill">
      <ControlsBar
        saving={saving}
        autoSave={autoSave}
        nodeCount={nodeCount}
        sotaId={tree.sota_id}
        onRefresh={refresh}
        onSave={save}
        onToggleAutoSave={toggleAutoSave}
      />
      {error && (
        <div className="callout callout--error" role="alert">
          <Icon name="alert" size={15} />
          <div className="callout__body">{error}</div>
        </div>
      )}
      <div className="graph-container" role="region" aria-label="研究树可视化" ref={viewport.containerRef}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={(_event, node) => setSelected(node.id)}
          onPaneClick={() => setSelected(null)}
          nodeTypes={nodeTypes}
          onInit={viewport.onInit}
          minZoom={0.2}
          maxZoom={2}
        >
          <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--border-strong)" />
          <Controls showInteractive={false} fitViewOptions={{ padding: 0.08 }} />
          <MiniMap
            pannable
            zoomable
            nodeColor={(n) => statusColor((n.data as ResearchNodeData).hypothesis.status).border}
            maskColor="rgba(0,0,0,0.06)"
          />
        </ReactFlow>
        <StatusLegend />
        {selectedData && <NodeDetail data={selectedData} onClose={() => setSelected(null)} />}
      </div>
    </div>
  );
}
