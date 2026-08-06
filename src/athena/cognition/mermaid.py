"""Mermaid graph renderer for Plan trees.

Pure function — Plan tree → Mermaid graph TD string.
"""

from athena.cognition.schemas import Plan, PlanNode, PlanNodeStatus

STATUS_ICON: dict[PlanNodeStatus, str] = {
    PlanNodeStatus.PENDING: "⬜",
    PlanNodeStatus.IN_PROGRESS: "🔄",
    PlanNodeStatus.COMPLETED: "✅",
    PlanNodeStatus.FAILED: "❌",
    PlanNodeStatus.SKIPPED: "⏭️",
}

RELATION_LABEL: dict[str, str] = {
    "sequential": "",
    "parallel": "[parallel]",
    "alternative": "[alternative]",
}


def _sanitize_id(node_id: str) -> str:
    """Replace Mermaid-unsafe characters in node IDs."""
    return node_id.replace("#", "n").replace(".", "_").replace("-", "_")


def _render_node(node: PlanNode) -> str:
    """Render a single PlanNode as a Mermaid node definition line."""
    safe_id = _sanitize_id(node.id)
    icon = STATUS_ICON[node.status]
    relation_label = RELATION_LABEL.get(node.relation, "")
    label = f"{icon} {node.title}"
    if relation_label:
        label += f" {relation_label}"
    # escape double quotes in label
    label = label.replace('"', "'")
    return f'    {safe_id}("{label}"):::{node.status.value}'


def _render_edges(node: PlanNode) -> list[str]:
    """Render edges from a node to its children."""
    edges: list[str] = []
    safe_parent = _sanitize_id(node.id)
    for child in node.children:
        safe_child = _sanitize_id(child.id)
        edge = f"    {safe_parent} --> {safe_child}"
        edges.append(edge)
        edges.extend(_render_edges(child))
    return edges


def _collect_nodes(node: PlanNode) -> list[PlanNode]:
    """Collect all nodes in a Plan tree (DFS)."""
    nodes = [node]
    for child in node.children:
        nodes.extend(_collect_nodes(child))
    return nodes


def render_mermaid(plan: Plan) -> str:
    """Render a Plan tree as a Mermaid graph TD string.

    Example output for a simple two-node plan:

        graph TD
            root("📋 完成任务"):::root
            step1("⬜ 第一步"):::pending

            root --> step1

            classDef root fill:#6b7280,color:#fff,stroke:#374151
            classDef completed fill:#22c55e,color:#fff,stroke:#16a34a
            classDef in_progress fill:#3b82f6,color:#fff,stroke:#2563eb
            classDef pending fill:#d1d5db,color:#374151,stroke:#9ca3af
            classDef failed fill:#ef4444,color:#fff,stroke:#dc2626
            classDef skipped fill:#9ca3af,color:#fff,stroke:#6b7280
    """
    lines: list[str] = ["graph TD"]
    all_nodes = _collect_nodes(plan.root)

    # node definitions
    for node in all_nodes:
        lines.append(_render_node(node))

    # blank separator
    lines.append("")

    # edges
    root_edges = _render_edges(plan.root)
    lines.extend(root_edges)

    # class definitions
    lines.append("")
    lines.append(
        "    classDef completed fill:#22c55e,color:#fff,stroke:#16a34a"
    )
    lines.append(
        "    classDef in_progress fill:#3b82f6,color:#fff,stroke:#2563eb"
    )
    lines.append(
        "    classDef pending fill:#d1d5db,color:#374151,stroke:#9ca3af"
    )
    lines.append(
        "    classDef failed fill:#ef4444,color:#fff,stroke:#dc2626"
    )
    lines.append(
        "    classDef skipped fill:#9ca3af,color:#fff,stroke:#6b7280"
    )

    return "\n".join(lines) + "\n"
