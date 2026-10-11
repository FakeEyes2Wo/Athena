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

export interface Viewport {
  x: number;
  y: number;
  zoom: number;
}

/**
 * Initial camera for a laid-out graph. Fits everything when the text stays
 * readable; otherwise opens at `readableZoom`, top-aligned on the root (the
 * topmost node) so a wide tree starts where its story starts instead of as an
 * illegible thumbnail. Positions are node top-left corners.
 */
export function initialViewport(
  positions: Position[],
  size: { width: number; height: number },
  opts: { nodeWidth: number; nodeHeight: number; readableZoom?: number; maxZoom?: number; padding?: number },
): Viewport {
  const { nodeWidth, nodeHeight } = opts;
  const readable = opts.readableZoom ?? 0.75;
  const maxZoom = opts.maxZoom ?? 1;
  const pad = opts.padding ?? 24;
  if (positions.length === 0) return { x: 0, y: 0, zoom: 1 };

  const minX = Math.min(...positions.map((p) => p.x));
  const minY = Math.min(...positions.map((p) => p.y));
  const maxX = Math.max(...positions.map((p) => p.x)) + nodeWidth;
  const maxY = Math.max(...positions.map((p) => p.y)) + nodeHeight;
  const graphW = maxX - minX;
  const graphH = maxY - minY;
  const fit = Math.min((size.width - 2 * pad) / graphW, (size.height - 2 * pad) / graphH, maxZoom);

  if (fit >= readable) {
    return {
      zoom: fit,
      x: (size.width - graphW * fit) / 2 - minX * fit,
      y: (size.height - graphH * fit) / 2 - minY * fit,
    };
  }

  const zoom = readable;
  const y = pad - minY * zoom;
  if (graphW * zoom <= size.width - 2 * pad) {
    return { zoom, y, x: (size.width - graphW * zoom) / 2 - minX * zoom };
  }
  const root = positions.reduce((top, p) => (p.y < top.y ? p : top), positions[0]);
  const centered = size.width / 2 - (root.x + nodeWidth / 2) * zoom;
  // Keep the graph's edges from drifting inside the viewport.
  const x = Math.min(pad - minX * zoom, Math.max(size.width - pad - maxX * zoom, centered));
  return { zoom, x, y };
}
