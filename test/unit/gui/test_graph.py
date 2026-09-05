"""Hypothesis graph projection and dispatch contracts."""

from copy import deepcopy

from athena.gui.graph import _dfs_cycles, build_hypothesis_graph, run_algorithm


class _SnapshotTree:
    def __init__(self) -> None:
        self.calls = 0
        self.data = {
            "sota_id": "exp_1",
            "hypotheses": {
                "h_1": {
                    "statement": "baseline",
                    "status": "TESTED",
                    "priority": 1.0,
                    "order": 0,
                    "supersedes": [],
                    "parent_id": None,
                    "sources": [],
                }
            },
            "experiments": {
                "exp_1": {
                    "hypothesis_id": "h_1",
                    "parent_id": None,
                    "eval": {"primary": 0.8},
                }
            },
        }

    def to_dict(self):
        self.calls += 1
        return deepcopy(self.data)


class _EmptyTree:
    def root_experiment_ids(self):
        return []

    def to_dict(self):
        return {"hypotheses": {}}


def test_graph_projection_uses_only_one_tree_snapshot() -> None:
    tree = _SnapshotTree()

    graph = build_hypothesis_graph(tree)

    assert tree.calls == 1
    assert graph["nodes"][0]["experiment_id"] == "exp_1"
    assert graph["nodes"][0]["sota"] is True


def test_parameterless_algorithm_dispatch_does_not_invent_an_argument() -> None:
    assert run_algorithm(_EmptyTree(), "topological_order", {}) == {"order": []}


def test_cycle_detector_reports_the_cycle_path() -> None:
    assert _dfs_cycles({"a": ["b"], "b": ["a"]}) == (
        False,
        [["a", "b"]],
    )
