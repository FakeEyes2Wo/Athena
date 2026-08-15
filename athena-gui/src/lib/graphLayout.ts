import { graphStratify, sugiyama, type GraphNode } from "d3-dag";

const VIRTUAL_ROOT = "__virtual_root__";

interface DagDatum {
  id: string;
  parentIds: string[];
}

interface LineageEdge {
  source: string;
  target: string;
}

export interface Position {
  x: number;
  y: number;
}

/**
 * Lay out nodes linked by lineage (single-parent tree) edges using d3-dag's
 * Sugiyama layered algorithm — parents centered over children, cross-edges
 * de-crossed, non-overlapping. Multiple roots and isolated nodes are joined to
 * a virtual root (dropped from the result) so the graph stays connected.
 */
export function layoutLineage(
  ids: string[],
  lineageEdges: LineageEdge[],
  opts: { nodeWidth?: number; nodeHeight?: number; gapX?: number; gapY?: number } = {},
): Map<string, Position> {
  const nodeWidth = opts.nodeWidth ?? 220;
  const nodeHeight = opts.nodeHeight ?? 96;
  const gapX = opts.gapX ?? 56;
  const gapY = opts.gapY ?? 84;

  const positions = new Map<string, Position>();
  if (ids.length === 0) return positions;

  // parentIds per node (lineage = single parent).
  const parents = new Map<string, string[]>(ids.map((id) => [id, []]));
  for (const edge of lineageEdges) {
    if (!parents.has(edge.target)) continue;
    parents.get(edge.target)!.push(edge.source);
  }

  const data: DagDatum[] = ids.map((id) => {
    const parentIds = parents.get(id) ?? [];
    return { id, parentIds: parentIds.length > 0 ? parentIds : [VIRTUAL_ROOT] };
  });
  data.push({ id: VIRTUAL_ROOT, parentIds: [] });

  const dag = graphStratify()(data);
  const layout = sugiyama()
    .nodeSize(
      (node: GraphNode<DagDatum, undefined>): readonly [number, number] =>
        node.data.id === VIRTUAL_ROOT ? [nodeWidth, 1] : [nodeWidth, nodeHeight],
    )
    .gap([gapX, gapY]);
  layout(dag);

  for (const node of dag.nodes()) {
    if (node.data.id === VIRTUAL_ROOT) continue;
    positions.set(node.data.id, { x: node.x, y: node.y });
  }
  return positions;
}
