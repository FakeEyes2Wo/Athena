import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Checkbox, Flash, FormControl } from "@primer/react";
import { errorMessage } from "../lib/errors";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
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
import { FlowNodeCard, GraphFrame, minimapNodeClass } from "./common/FlowNodeCard";
import { Panel } from "./common/Panel";

const AUTO_SAVE_MS = 30_000;
const NODE_W = 220;
const NODE_H = 92;

/** Custom React Flow node rendering a research experiment with hypothesis status. */
interface ResearchNodeData {
  id: string;
  experiment: ResearchExperimentData;
  hypothesis: HypothesisData;
}

function ResearchNode({ data }: { data: ResearchNodeData }) {
  return (
    <FlowNodeCard
      statement={data.hypothesis.statement}
      primaryText={data.experiment.eval ? data.experiment.eval.primary.toFixed(4) : "无指标"}
      status={data.hypothesis.status}
      badge={data.experiment.status}
    />
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
    markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: "var(--fgColor-muted)" },
    style: { stroke: "var(--fgColor-muted)", strokeWidth: 1.75 },
  }));

  return { nodes, edges };
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

  const actions = (
    <>
      <Button size="small" onClick={() => void refresh()}>
        刷新
      </Button>
      <Button size="small" onClick={() => void save()} disabled={saving}>
        {saving ? "保存中…" : "保存"}
      </Button>
      <FormControl>
        <Checkbox checked={autoSave} onChange={toggleAutoSave} />
        <FormControl.Label>自动保存</FormControl.Label>
      </FormControl>
    </>
  );

  if (!tree || nodeCount === 0) {
    return (
      <Panel title="研究树 (0 个实验)" actions={actions}>
        {error && <Flash variant="danger">{error}</Flash>}
        <EmptyState icon="tree" message="暂无实验数据。启动搜索后研究树将自动构建。" />
      </Panel>
    );
  }

  return (
    <Panel fill title={`研究树 (${nodeCount} 个实验)`} actions={actions}>
      <GraphFrame label="研究树可视化">
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
          <MiniMap nodeClassName={(n) => minimapNodeClass((n.data as ResearchNodeData).hypothesis.status)} />
        </ReactFlow>
      </GraphFrame>
    </Panel>
  );
}
