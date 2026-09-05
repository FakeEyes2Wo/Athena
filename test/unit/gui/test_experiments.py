"""GUI experiment query contracts."""

from copy import deepcopy

from athena.gui.experiments import list_experiments


class _Tree:
    def __init__(self) -> None:
        self.calls = 0
        self.data = {
            "sota_id": "exp_early",
            "hypotheses": {
                "h_late": {"order": 2},
                "h_early": {"order": 1},
            },
            "experiments": {
                "exp_late": {
                    "hypothesis_id": "h_late",
                    "plan": {"kind": "SEARCH"},
                },
                "exp_early": {
                    "hypothesis_id": "h_early",
                    "plan": {"kind": "SEARCH"},
                },
            },
        }

    def to_dict(self):
        self.calls += 1
        return deepcopy(self.data)


def test_list_experiments_uses_one_ordered_tree_snapshot() -> None:
    tree = _Tree()

    details = list_experiments(tree, "SEARCH")

    assert tree.calls == 1
    assert [detail["experiment"]["id"] for detail in details] == [
        "exp_early",
        "exp_late",
    ]
    assert details[0]["sota"] is True
