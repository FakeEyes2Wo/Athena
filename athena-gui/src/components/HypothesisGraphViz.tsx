import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Flash, Label } from "@primer/react";
import { errorMessage } from "../lib/errors";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  MarkerType,
  type Node,
  type Edge,
  useNodesState,
  useEdgesState,
} from "reactflow";
import "reactflow/dist/style.css";
import { hypothesisGraph, type HypGraph, type HypGraphNode } from "../lib/tauri-bridge";
import { layoutLineage } from "../lib/graphLayout";
import { EmptyState } from "./common/EmptyState";
import { FlowNodeCard, GraphFrame, minimapNodeClass } from "./common/FlowNodeCard";
import { HYPOTHESIS_STATUS_VARIANT } from "./common/hypothesisStatus";
import { Panel } from "./common/Panel";
import styles from "./HypothesisGraphViz.module.css";

const NODE_W = 220;
const NODE_H = 96;

interface HypNodeData {
  node: HypGraphNode;
}

/** Custom React Flow node for a single hypothesis. */
function HypNode({ data }: { data: HypNodeData }) {
  return (
    <FlowNodeCard
      statement={data.node.statement}
      primaryText={data.node.primary != null ? data.node.primary.toFixed(4) : "未安排实验"}
      status={data.node.status}
      badge={`${data.node.status}${data.node.sota ? " ★ SOTA" : ""}`}
    />
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

  const nodes: Node[] = graph.nodes.map((node) => {
    const pos = positions.get(node.id) ?? { x: 0, y: 0 };
    return {
      id: node.id,
      type: "hypNode",
      position: pos,
      data: { node },
    };
  });

  const edges: Edge[] = graph.edges.map((edge) => {
    const supersedes = edge.kind === "supersedes";
    const color = supersedes ? "var(--fgColor-danger)" : "var(--fgColor-muted)";
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: edge.label,
      style: {
        stroke: color,
        strokeWidth: supersedes ? 1.5 : 1.75,
        strokeDasharray: supersedes ? "6 4" : undefined,
      },
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color },
    };
  });

  return { nodes, edges };
}

/** Legend describing edge and status coloring. */
function Legend() {
  return (
    <div className={styles.legend}>
      <span className={styles.legendTitle}>图例</span>
      <span className={styles.legendRow}>
        <span className={styles.legendLine} />
        谱系 (lineage)
      </span>
      <span className={styles.legendRow}>
        <span className={`${styles.legendLine} ${styles.legendLineDashed}`} />
        取代 (supersedes)
      </span>
      <span className={styles.legendSwatches}>
        {Object.entries(HYPOTHESIS_STATUS_VARIANT).map(([status, variant]) => (
          <Label key={status} variant={variant} size="small">
            {status}
          </Label>
        ))}
      </span>
    </div>
  );
}

/** Interactive hypothesis-graph visualization with lineage + supersedes edges. */
export default function HypothesisGraphViz() {
  const [graphData, setGraphData] = useState<HypGraph | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const result = await hypothesisGraph();
      setGraphData(result);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

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

  const nodeCount = graphData?.nodes.length ?? 0;

  return (
    <Panel
      fill
      title={`假设图 (${nodeCount} 个假设)`}
      actions={
        <Button size="small" onClick={() => void refresh()} disabled={loading}>
          {loading ? "加载中…" : "刷新"}
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {!graphData || nodeCount === 0 ? (
        <EmptyState icon="network" message="暂无假设图数据。启动搜索产生假设后，此处将展示谱系与取代关系。" />
      ) : (
        <GraphFrame label="假设图可视化">
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
            <Background variant={BackgroundVariant.Dots} gap={24} size={1} />
            <Controls />
            <MiniMap nodeClassName={(n) => minimapNodeClass((n.data as HypNodeData).node.status)} />
          </ReactFlow>
          <Legend />
        </GraphFrame>
      )}
    </Panel>
  );
}
