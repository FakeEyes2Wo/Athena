import type { ResearchTree } from "./research-tree.js"

/** Hypothesis-graph construction (mirrors ``src/athena/gui/graph.py``). */
export interface HypothesisGraphNode {
  id: string
  experiment_id: string | null
  statement: string
  status: string
  priority: number
  order: number | null
  supersedes: string[]
  parent_id: string | null
  sources: string[]
  primary: number | null
  sota: boolean
}

export interface HypothesisGraphEdge {
  id: string
  source: string
  target: string
  kind: "lineage" | "supersedes"
  label: string
}

export interface HypothesisGraph {
  nodes: HypothesisGraphNode[]
  edges: HypothesisGraphEdge[]
}

/** Build ``{"nodes": [...], "edges": [...]}`` for the hypothesis graph.

 * Edge kinds:
 * - ``lineage`` — parent experiment hypothesis → child experiment hypothesis.
 * - ``supersedes`` — child hypothesis → superseded ancestor hypothesis.
 */
export function buildHypothesisGraph(tree: ResearchTree): HypothesisGraph {
  const data = tree.toDict()
  const hypotheses = data.hypotheses
  const experiments = data.experiments
  const sotaId = data.sota_id

  const expToHyp = new Map<string, string>()
  for (const [experimentId, experiment] of Object.entries(experiments)) {
    expToHyp.set(experimentId, experiment.hypothesis_id)
  }

  const nodes: HypothesisGraphNode[] = Object.entries(hypotheses).map(
    ([hypothesisId, hypothesis]) => {
      const experimentId = tree.experimentForHypothesis(hypothesisId)
      const experiment =
        experimentId !== null ? experiments[experimentId] : undefined
      const primary =
        experiment?.eval?.primary !== undefined ? experiment.eval.primary : null
      return {
        id: hypothesisId,
        experiment_id: experimentId,
        statement: hypothesis.statement,
        status: hypothesis.status,
        priority: hypothesis.priority,
        order: hypothesis.order,
        supersedes: [...hypothesis.supersedes],
        parent_id: hypothesis.parent_id,
        sources: [...hypothesis.sources],
        primary,
        sota: experimentId !== null && experimentId === sotaId,
      }
    },
  )

  const edges: HypothesisGraphEdge[] = []
  for (const [, experiment] of Object.entries(experiments)) {
    const parentHypothesis =
      experiment.parent_id !== null
        ? expToHyp.get(experiment.parent_id)
        : undefined
    if (parentHypothesis === undefined) continue
    const target = experiment.hypothesis_id
    edges.push({
      id: `lineage:${parentHypothesis}->${target}`,
      source: parentHypothesis,
      target,
      kind: "lineage",
      label: "父→子",
    })
  }
  for (const [hypothesisId, hypothesis] of Object.entries(hypotheses)) {
    for (const superseded of hypothesis.supersedes) {
      edges.push({
        id: `supersedes:${hypothesisId}->${superseded}`,
        source: hypothesisId,
        target: superseded,
        kind: "supersedes",
        label: "取代",
      })
    }
  }

  return { nodes, edges }
}
