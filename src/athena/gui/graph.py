"""Hypothesis-graph construction and algorithms (pure functions over ``ResearchTree``).

The hypothesis graph has two edge kinds over hypothesis nodes:

* ``lineage``   — 父实验假设 → 子实验假设（研究树父子谱系）。
* ``supersedes``— 子假设 → 其 ``supersedes`` 列表中的祖先假设。

Every function here reads the tree via its public API (``to_dict`` / ``get_*`` /
``experiment_for_hypothesis`` / …) and returns plain JSON dicts, so the frontend
can mirror them without importing the runtime.
"""

from typing import Any

from athena.core.research_tree import ResearchTree


def build_hypothesis_graph(tree: ResearchTree) -> dict[str, Any]:
    """Return ``{"nodes": [...], "edges": [...]}`` for the hypothesis graph.

    Node keys are hypothesis ids (1:1 with experiments; a hypothesis may have no
    experiment yet, in which case ``experiment_id`` is ``None``).
    """
    data = tree.to_dict()
    hypotheses: dict[str, dict[str, Any]] = data["hypotheses"]
    experiments: dict[str, dict[str, Any]] = data["experiments"]
    sota_id = data.get("sota_id")

    exp_to_hyp = {
        exp_id: exp["hypothesis_id"] for exp_id, exp in experiments.items()
    }

    nodes: list[dict[str, Any]] = []
    for hyp_id, hyp in hypotheses.items():
        exp_id = tree.experiment_for_hypothesis(hyp_id)
        primary = None
        if exp_id is not None and experiments.get(exp_id, {}).get("eval"):
            primary = experiments[exp_id]["eval"]["primary"]
        nodes.append(
            {
                "id": hyp_id,
                "experiment_id": exp_id,
                "statement": hyp["statement"],
                "status": hyp["status"],
                "priority": hyp["priority"],
                "order": hyp["order"],
                "supersedes": list(hyp["supersedes"]),
                "parent_id": hyp["parent_id"],
                "sources": list(hyp["sources"]),
                "primary": primary,
                "sota": exp_id == sota_id if exp_id is not None else False,
            }
        )

    edges: list[dict[str, Any]] = []
    for exp_id, exp in experiments.items():
        parent_exp = exp["parent_id"]
        if parent_exp is None or parent_exp not in exp_to_hyp:
            continue
        source = exp_to_hyp[parent_exp]
        target = exp["hypothesis_id"]
        edges.append(
            {
                "id": f"lineage:{source}->{target}",
                "source": source,
                "target": target,
                "kind": "lineage",
                "label": "父→子",
            }
        )
    for hyp_id, hyp in hypotheses.items():
        for superseded in hyp["supersedes"]:
            edges.append(
                {
                    "id": f"supersedes:{hyp_id}->{superseded}",
                    "source": hyp_id,
                    "target": superseded,
                    "kind": "supersedes",
                    "label": "取代",
                }
            )
    return {"nodes": nodes, "edges": edges}


def topological_order(tree: ResearchTree) -> dict[str, Any]:
    """BFS 层序排序实验树假设；无实验登记的假设按 ``order`` 补在末尾。"""
    order: list[str] = []
    visited: set[str] = set()
    queue = list(tree.root_experiment_ids())
    while queue:
        exp_id = queue.pop(0)
        if exp_id in visited:
            continue
        visited.add(exp_id)
        order.append(tree.get_experiment(exp_id).hypothesis_id)
        queue.extend(tree.list_children(exp_id))

    data = tree.to_dict()
    hypotheses = data["hypotheses"]

    def sort_key(hyp_id: str) -> tuple[bool, int]:
        order_val = hypotheses[hyp_id].get("order")
        return (order_val is None, order_val if order_val is not None else 0)

    for hyp_id in sorted(hypotheses, key=sort_key):
        if hyp_id not in order:
            order.append(hyp_id)
    return {"order": order}


def _dfs_cycles(adjacency: dict[str, list[str]]) -> tuple[bool, list[list[str]]]:
    """Three-color DFS returning ``(acyclic, cycles)`` over one directed graph."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in adjacency}
    cycles: list[list[str]] = []
    stack: list[str] = []

    def dfs(node: str) -> bool:
        color[node] = GRAY
        stack.append(node)
        for neighbor in adjacency[node]:
            if color[neighbor] == WHITE:
                if dfs(neighbor):
                    return True
            elif color[neighbor] == GRAY:
                index = stack.index(neighbor)
                cycles.append(stack[index:])
        stack.pop()
        color[node] = BLACK
        return False

    for node in adjacency:
        if color[node] == WHITE:
            dfs(node)
    return not cycles, cycles


def cycle_detect(tree: ResearchTree) -> dict[str, Any]:
    """Detect cycles in the lineage and supersedes graphs separately.

    The two edge kinds point in opposite directions along the tree (lineage =
    parent→child, supersedes = child→superseded ancestor), so their union
    naturally contains 2-cycles and is not the meaningful acyclicity target.
    A well-formed tree has an acyclic lineage (a forest) and an acyclic
    supersedes relation (a DAG pointing only to ancestors).
    """
    graph = build_hypothesis_graph(tree)
    results: dict[str, Any] = {}
    overall = True
    for kind in ("lineage", "supersedes"):
        adjacency: dict[str, list[str]] = {n["id"]: [] for n in graph["nodes"]}
        for edge in graph["edges"]:
            if edge["kind"] == kind:
                adjacency[edge["source"]].append(edge["target"])
        acyclic, cycles = _dfs_cycles(adjacency)
        results[kind] = {"acyclic": acyclic, "cycles": cycles}
        overall = overall and acyclic
    results["acyclic"] = overall
    return results


def lineage(tree: ResearchTree, params: dict[str, Any]) -> dict[str, Any]:
    """Ancestor experiment chain (root → target) and its hypotheses."""
    exp_id = params["experiment_id"]
    return {
        "path": tree.experiment_path(exp_id),
        "hypotheses": [
            h.model_dump(mode="json") for h in tree.hypotheses_path(exp_id)
        ],
    }


def active_hypotheses(tree: ResearchTree, params: dict[str, Any]) -> dict[str, Any]:
    """Active hypothesis set for a selected parent experiment + child hypothesis.

    = ancestor hypotheses minus superseded claims, plus the child.
    """
    exp_id = params["experiment_id"]
    child_id = params["child_hypothesis_id"]
    child = tree.get_hypothesis(child_id)
    active = tree.active_hypotheses(exp_id, child)
    superseded = [
        h.id for h in tree.hypotheses_path(exp_id) if h.id in child.supersedes
    ]
    return {
        "active": [h.model_dump(mode="json") for h in active],
        "superseded": superseded,
    }


def descendants(tree: ResearchTree, params: dict[str, Any]) -> dict[str, Any]:
    """BFS 后代实验 id 列表。"""
    return {"descendants": tree.list_descendants(params["experiment_id"])}


def best_path(tree: ResearchTree) -> dict[str, Any]:
    """Current SOTA experiment and its ancestor chain."""
    sota_id = tree.best_experiment_id()
    if sota_id is None:
        return {"sota_id": None, "path": [], "hypotheses": []}
    return {
        "sota_id": sota_id,
        "path": tree.experiment_path(sota_id),
        "hypotheses": [
            h.model_dump(mode="json") for h in tree.hypotheses_path(sota_id)
        ],
    }


def rank_pending(tree: ResearchTree) -> dict[str, Any]:
    """Rank PROPOSED hypotheses by priority ascending (smaller = higher)."""
    pending = sorted(
        tree.pending_hypotheses(),
        key=lambda h: (h.priority, h.order if h.order is not None else 0),
    )
    return {
        "ranked": [
            {"id": h.id, "priority": h.priority, "statement": h.statement}
            for h in pending
        ]
    }


def supersedes_closure(tree: ResearchTree, params: dict[str, Any]) -> dict[str, Any]:
    """Transitive closure of the ``supersedes`` relation from one hypothesis."""
    hyp_id = params["hypothesis_id"]
    closure: list[str] = []
    seen: set[str] = set()
    queue = list(tree.get_hypothesis(hyp_id).supersedes)
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        closure.append(current)
        try:
            queue.extend(tree.get_hypothesis(current).supersedes)
        except KeyError:
            continue
    return {"hypothesis_id": hyp_id, "closure": closure}


def refutation_reachability(tree: ResearchTree, params: dict[str, Any]) -> dict[str, Any]:
    """Hypotheses affected downstream if a hypothesis is refuted (lineage propagation)."""
    hyp_id = params["hypothesis_id"]
    exp_id = tree.experiment_for_hypothesis(hyp_id)
    if exp_id is None:
        return {"hypothesis_id": hyp_id, "affected": []}
    affected = [
        tree.get_experiment(desc).hypothesis_id
        for desc in tree.list_descendants(exp_id)
    ]
    return {"hypothesis_id": hyp_id, "affected": affected}


ALGORITHMS: list[dict[str, Any]] = [
    {"name": "topological_order", "label": "拓扑排序（BFS 层序）", "params": []},
    {"name": "cycle_detect", "label": "环检测", "params": []},
    {
        "name": "lineage",
        "label": "祖先谱系",
        "params": [{"name": "experiment_id", "type": "string", "required": True}],
    },
    {
        "name": "active_hypotheses",
        "label": "活跃假设集（去取代）",
        "params": [
            {"name": "experiment_id", "type": "string", "required": True},
            {"name": "child_hypothesis_id", "type": "string", "required": True},
        ],
    },
    {
        "name": "descendants",
        "label": "后代实验",
        "params": [{"name": "experiment_id", "type": "string", "required": True}],
    },
    {"name": "best_path", "label": "最佳路径（SOTA 链）", "params": []},
    {"name": "rank_pending", "label": "待选假设按优先级排序", "params": []},
    {
        "name": "supersedes_closure",
        "label": "取代传递闭包",
        "params": [{"name": "hypothesis_id", "type": "string", "required": True}],
    },
    {
        "name": "refutation_reachability",
        "label": "证伪影响范围",
        "params": [{"name": "hypothesis_id", "type": "string", "required": True}],
    },
]

_HANDLERS = {
    "topological_order": topological_order,
    "cycle_detect": cycle_detect,
    "lineage": lineage,
    "active_hypotheses": active_hypotheses,
    "descendants": descendants,
    "best_path": best_path,
    "rank_pending": rank_pending,
    "supersedes_closure": supersedes_closure,
    "refutation_reachability": refutation_reachability,
}


def run_algorithm(tree: ResearchTree, name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one algorithm by name, raising ``KeyError`` for unknown names."""
    handler = _HANDLERS.get(name)
    if handler is None:
        raise KeyError(f"unknown algorithm: {name}")
    return handler(tree, params)
