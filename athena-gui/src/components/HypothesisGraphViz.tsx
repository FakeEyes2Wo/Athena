import { useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MiniMap,
  Position,
  MarkerType,
  type Node,
  type Edge,
  useNodesState,
  useEdgesState,
} from "reactflow";
import "reactflow/dist/style.css";
import { hypothesisGraph, type HypGraph, type HypGraphNode } from "../lib/tauri-bridge";
import { layoutLineage } from "../lib/graphLayout";
import { formatMetric } from "../lib/labels";
import { useRemoteData } from "../hooks/useRemoteData";
import { useInitialViewport } from "../hooks/useInitialViewport";
import { StatusLegend, statusColor } from "./ResearchTreeViz";
import { EmptyState } from "./common/EmptyState";
import { Icon } from "./common/Icon";
import { HYPOTHESIS_STATUS, StatusBadge } from "./common/StatusBadge";

const NODE_W = 220;
const NODE_H = 96;

interface HypNodeData {
  node: HypGraphNode;
}

/** Custom React Flow node for a single hypothesis. */
function HypNode({ data }: { data: HypNodeData }) {
  const colors = statusColor(data.node.status);
  const primary = data.node.primary != null ? data.node.primary.toFixed(4) : "未安排实验";

  return (
    <div className={`flow-node${data.node.sota ? " flow-node--sota" : ""}`} style={{ borderLeftColor: colors.border }}>
      <Handle type="target" position={Position.Top} />
      <div className="flow-node__title" title={data.node.statement}>
        {data.node.statement}
      </div>
      <div className="flow-node__meta">
        <span>
          {data.node.sota && <span style={{ color: "var(--gold)" }}>★ </span>}
          <strong>{primary}</strong>
        </span>
        <span className="flow-node__badge" style={{ background: colors.bg, color: colors.text }}>
          {HYPOTHESIS_STATUS[data.node.status]?.label ?? data.node.status}
        </span>
      </div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { hypNode: HypNode };

/** Convert the hypothesis graph DTO into React Flow nodes/edges laid out by d3-dag. */
function buildHypGraph(graph: HypGraph): { nodes: Node[]; edges: Edge[] } {
  const lineageEdges = graph.edges.filter((e) => e.kind === "lineage");
  const positions = layoutLineage(
    graph.nodes.map((n) => n.id),
    lineageEdges.map((e) => ({ source: e.source, target: e.target })),
    { nodeWidth: NODE_W, nodeHeight: NODE_H },
  );

  const nodes: Node[] = graph.nodes.map((node) => ({
    id: node.id,
    type: "hypNode",
    position: positions.get(node.id) ?? { x: 0, y: 0 },
    data: { node },
  }));

  const edges: Edge[] = graph.edges.map((edge) => {
    const supersedes = edge.kind === "supersedes";
    const color = supersedes ? "var(--danger)" : "var(--text-tertiary)";
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      // Lineage is explained by the legend; labelling every edge only adds noise.
      label: supersedes ? edge.label : undefined,
      style: {
        stroke: color,
        strokeWidth: 1.5,
        strokeDasharray: supersedes ? "6 4" : undefined,
      },
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color },
    };
  });

  return { nodes, edges };
}

/** Interactive hypothesis graph with lineage and supersedes relations. */
export default function HypothesisGraphViz() {
  const { data: graphData, error, loading, refresh } = useRemoteData(() => hypothesisGraph());
  const [selected, setSelected] = useState<string | null>(null);

  const { nodes: builtNodes, edges: builtEdges } = useMemo(
    () => (graphData ? buildHypGraph(graphData) : { nodes: [] as Node[], edges: [] as Edge[] }),
    [graphData],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(builtNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(builtEdges);

  useEffect(() => {
    setNodes(builtNodes);
    setEdges(builtEdges);
  }, [builtNodes, builtEdges, setNodes, setEdges]);

  const viewport = useInitialViewport(builtNodes, { nodeWidth: NODE_W, nodeHeight: NODE_H });
  const nodeCount = graphData?.nodes.length ?? 0;
  const selectedNode = graphData?.nodes.find((node) => node.id === selected) ?? null;

  return (
    <div className={`detail-panel${nodeCount > 0 ? " detail-panel--fill" : ""}`}>
      <div className="panel-toolbar">
        <h3>
          {nodeCount} 个假设
          <span className="panel-toolbar__count">· {graphData?.edges.length ?? 0} 条关系</span>
        </h3>
        <button type="button" className="btn btn--ghost btn--sm" onClick={() => void refresh()} disabled={loading}>
          <Icon name="refresh" size={13} className={loading ? "icon--spin" : undefined} /> 刷新
        </button>
      </div>

      {error && (
        <div className="callout callout--error" role="alert">
          <Icon name="alert" size={15} />
          <div className="callout__body">{error}</div>
        </div>
      )}

      {!graphData || nodeCount === 0 ? (
        <EmptyState
          icon={loading && !graphData ? "loader" : "network"}
          title={loading && !graphData ? undefined : "暂无假设"}
          message={
            loading && !graphData
              ? "加载中…"
              : "搜索阶段产生假设后，这里会展示它们之间的谱系（由哪个实验派生）与取代关系。"
          }
        />
      ) : (
        <div className="graph-container" role="region" aria-label="假设图可视化" ref={viewport.containerRef}>
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
              nodeColor={(n) => statusColor((n.data as HypNodeData).node.status).border}
              maskColor="rgba(0,0,0,0.06)"
            />
          </ReactFlow>
          <StatusLegend>
            <span className="legend__row">
              <span className="legend__line" /> 谱系（由父实验派生）
            </span>
            <span className="legend__row">
              <span className="legend__line legend__line--dashed" /> 取代
            </span>
          </StatusLegend>
          {selectedNode && (
            <div className="graph-detail" role="region" aria-label="节点详情">
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, fontWeight: 600, flex: 1 }}>{selectedNode.id}</span>
                <button type="button" className="icon-btn icon-btn--sm" onClick={() => setSelected(null)} aria-label="关闭节点详情">
                  <Icon name="close" size={14} />
                </button>
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                <StatusBadge status={selectedNode.status} kind="hypothesis" />
                {selectedNode.sota && <span className="badge badge--gold">★ SOTA</span>}
              </div>
              <p style={{ margin: 0, lineHeight: 1.6 }}>{selectedNode.statement}</p>
              <div className="kv">
                <span className="kv__label">主指标</span>
                <span className="kv__value kv__value--mono">{formatMetric(selectedNode.primary)}</span>
              </div>
              <div className="kv">
                <span className="kv__label">对应实验</span>
                <span className="kv__value kv__value--mono">{selectedNode.experiment_id ?? "—"}</span>
              </div>
              {selectedNode.supersedes.length > 0 && (
                <div className="kv">
                  <span className="kv__label">取代</span>
                  <span className="kv__value kv__value--mono">{selectedNode.supersedes.join("、")}</span>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
