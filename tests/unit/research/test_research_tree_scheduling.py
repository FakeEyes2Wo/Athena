"""Scheduling and selective-inheritance behavior on the existing ResearchTree."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena.core.research_models import Hypothesis
from athena.core.research_tree import Experiment, ResearchTree
from athena.core.workspace import GitWorkBranch

LEGACY_V2_FIXTURE = Path("tests/fixtures/research_tree_v2.json")


def _hypothesis(
    hypothesis_id: str,
    *,
    parent_id: str | None = None,
    supersedes: list[str] | None = None,
    priority: float = 1000.0,
    order: int | None = None,
) -> Hypothesis:
    return Hypothesis(
        id=hypothesis_id,
        parent_id=parent_id,
        statement=f"claim {hypothesis_id}",
        intervention=f"change {hypothesis_id}",
        expected_effect="improve the trusted metric",
        supersedes=[] if supersedes is None else supersedes,
        priority=priority,
        order=order,
        patience=5,
        turn_limit=12,
    )


def _experiment(hypothesis_id: str, parent_id: str | None = None) -> Experiment:
    return Experiment.model_validate(
        {
            "parent_id": parent_id,
            "hypothesis_id": hypothesis_id,
            "commit": "abc123",
            "plan": {
                "kind": "search",
                "change": f"test {hypothesis_id}",
                "run_config_ref": "artifact://run-config",
                "budget": {},
                "acceptance_rule": "trusted evaluation completes",
            },
            "gitwork": {
                "path": f"C:/worktrees/{hypothesis_id}",
                "branch": f"search/{hypothesis_id}",
                "base_commit": "base123",
            },
        }
    )


def _tree_with_path(*hypothesis_ids: str) -> ResearchTree:
    tree = ResearchTree()
    parent_experiment_id: str | None = None
    for hypothesis_id in hypothesis_ids:
        tree.add_hypothesis(_hypothesis(hypothesis_id, parent_id=parent_experiment_id))
        experiment_id = f"exp_{hypothesis_id.removeprefix('h_')}"
        tree.add_experiment(
            experiment_id,
            _experiment(hypothesis_id, parent_id=parent_experiment_id),
        )
        parent_experiment_id = experiment_id
    return tree


def test_supersedes_only_removes_named_ancestor_hypotheses() -> None:
    tree = _tree_with_path("h_data", "h_feature", "h_xgb", "h_depth")
    child = _hypothesis(
        "h_vit",
        parent_id="exp_depth",
        supersedes=["h_xgb", "h_depth"],
        order=5,
    )

    assert [
        hypothesis.id for hypothesis in tree.active_hypotheses("exp_depth", child)
    ] == ["h_data", "h_feature", "h_vit"]


@pytest.mark.parametrize(
    ("supersedes", "message"),
    [
        (["h_xgb", "h_xgb"], "duplicate"),
        (["h_vit"], "child hypothesis"),
        (["h_unrelated"], "selected parent experiment path"),
    ],
)
def test_active_hypotheses_rejects_invalid_supersedes(
    supersedes: list[str], message: str
) -> None:
    tree = _tree_with_path("h_data", "h_xgb")
    tree.add_hypothesis(_hypothesis("h_unrelated"))
    child = _hypothesis(
        "h_vit",
        parent_id="exp_xgb",
        supersedes=supersedes,
    )

    with pytest.raises(ValueError, match=message):
        tree.active_hypotheses("exp_xgb", child)


def test_active_hypotheses_rejects_a_different_selected_parent() -> None:
    tree = _tree_with_path("h_data", "h_xgb")
    tree.add_hypothesis(_hypothesis("h_other"))
    tree.add_experiment("exp_other", _experiment("h_other"))
    child = _hypothesis("h_vit", parent_id="exp_xgb")

    with pytest.raises(ValueError, match="parent experiment"):
        tree.active_hypotheses("exp_other", child)


def test_registration_rejects_dangling_hypothesis_parent() -> None:
    tree = ResearchTree()

    with pytest.raises(KeyError, match="unknown parent experiment"):
        tree.add_hypothesis(_hypothesis("h_child", parent_id="exp_missing"))


def test_experiment_parent_must_match_hypothesis_parent() -> None:
    tree = _tree_with_path("h_root")
    tree.add_hypothesis(_hypothesis("h_child", parent_id="exp_root"))

    with pytest.raises(ValueError, match="does not match hypothesis parent"):
        tree.add_experiment("exp_child", _experiment("h_child", parent_id=None))


def test_load_rejects_dangling_hypothesis_parent() -> None:
    payload = json.loads(LEGACY_V2_FIXTURE.read_text(encoding="utf-8"))
    payload["hypotheses"]["hyp_child"]["parent_id"] = "exp_missing"

    with pytest.raises(KeyError, match="unknown parent experiment"):
        ResearchTree.from_dict(payload)


def test_load_rejects_experiment_hypothesis_parent_mismatch() -> None:
    payload = json.loads(LEGACY_V2_FIXTURE.read_text(encoding="utf-8"))
    payload["hypotheses"]["hyp_child"]["parent_id"] = None

    with pytest.raises(ValueError, match="does not match hypothesis parent"):
        ResearchTree.from_dict(payload)


def test_one_hypothesis_cannot_have_two_experiments() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(_hypothesis("h1"))
    tree.add_experiment("e1", _experiment("h1"))

    with pytest.raises(ValueError, match="already has an experiment"):
        tree.add_experiment("e2", _experiment("h1"))


def test_loading_rejects_two_experiments_for_one_hypothesis() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(_hypothesis("h1"))
    tree.add_experiment("e1", _experiment("h1"))
    payload = tree.to_dict()
    payload["experiments"]["e2"] = payload["experiments"]["e1"]

    with pytest.raises(ValueError, match="already has an experiment"):
        ResearchTree.from_dict(payload)


def test_experiment_for_hypothesis_returns_the_registered_experiment() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(_hypothesis("h1"))
    tree.add_experiment("e1", _experiment("h1"))

    assert tree.experiment_for_hypothesis("h1") == "e1"
    assert tree.experiment_for_hypothesis("missing") is None


def test_registration_assigns_stable_fifo_order_only_when_absent() -> None:
    tree = ResearchTree()

    tree.add_hypothesis(_hypothesis("h_explicit", order=8))
    tree.add_hypothesis(_hypothesis("h_first"))
    tree.add_hypothesis(_hypothesis("h_second"))

    assert tree.get_hypothesis("h_explicit").order == 8
    assert tree.get_hypothesis("h_first").order == 9
    assert tree.get_hypothesis("h_second").order == 10


def test_registration_rejects_duplicate_explicit_order() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(_hypothesis("h_first", order=4))

    with pytest.raises(ValueError, match="duplicate hypothesis order"):
        tree.add_hypothesis(_hypothesis("h_second", order=4))


def test_load_rejects_duplicate_explicit_order() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(_hypothesis("h_first", order=4))
    tree.add_hypothesis(_hypothesis("h_second", order=5))
    payload = tree.to_dict()
    payload["hypotheses"]["h_second"]["order"] = 4

    with pytest.raises(ValueError, match="duplicate hypothesis order"):
        ResearchTree.from_dict(payload)


def test_v2_payload_without_scheduling_fields_loads_with_compatible_defaults() -> None:
    tree = _tree_with_path("h_root")
    payload = tree.to_dict()
    payload["version"] = 2
    raw_hypothesis = payload["hypotheses"]["h_root"]
    for field in ("supersedes", "priority", "order", "patience", "turn_limit"):
        raw_hypothesis.pop(field, None)

    loaded = ResearchTree.from_dict(payload)
    hypothesis = loaded.get_hypothesis("h_root")

    assert hypothesis.supersedes == []
    assert hypothesis.priority == 1000.0
    assert hypothesis.order == 0
    assert hypothesis.patience == 0
    assert hypothesis.turn_limit is None


def test_v2_payload_assigns_missing_orders_in_mapping_order() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(_hypothesis("h_first"))
    tree.add_hypothesis(_hypothesis("h_second"))
    payload = tree.to_dict()
    payload["version"] = 2
    payload["hypotheses"]["h_first"].pop("order")
    payload["hypotheses"]["h_second"].pop("order")

    loaded = ResearchTree.from_dict(payload)

    assert loaded.get_hypothesis("h_first").order == 0
    assert loaded.get_hypothesis("h_second").order == 1


def test_reordered_legacy_v2_payload_assigns_orders_in_payload_order() -> None:
    payload = json.loads(LEGACY_V2_FIXTURE.read_text(encoding="utf-8"))
    payload["hypotheses"] = {
        "hyp_child": payload["hypotheses"]["hyp_child"],
        "hyp_baseline": payload["hypotheses"]["hyp_baseline"],
    }

    loaded = ResearchTree.from_dict(payload)

    assert loaded.get_hypothesis("hyp_child").order == 0
    assert loaded.get_hypothesis("hyp_baseline").order == 1


def test_legacy_v2_fixture_save_migrates_scheduling_fields_to_v3(
    tmp_path: Path,
) -> None:
    original = json.loads(LEGACY_V2_FIXTURE.read_text(encoding="utf-8"))
    tree = ResearchTree.load(LEGACY_V2_FIXTURE)
    target = tmp_path / "research_tree.json"

    tree.save(target)

    migrated = json.loads(target.read_text(encoding="utf-8"))
    assert migrated["version"] == 3
    assert migrated["experiments"] == original["experiments"]
    assert migrated["hypotheses"]["hyp_baseline"]["order"] == 0
    assert migrated["hypotheses"]["hyp_child"]["order"] == 1

    migrated["hypotheses"] = {
        "hyp_child": migrated["hypotheses"]["hyp_child"],
        "hyp_baseline": migrated["hypotheses"]["hyp_baseline"],
    }
    reloaded = ResearchTree.from_dict(migrated)
    assert reloaded.get_hypothesis("hyp_baseline").order == 0
    assert reloaded.get_hypothesis("hyp_child").order == 1


@pytest.mark.parametrize("priority", [math.nan, math.inf, -math.inf])
def test_hypothesis_priority_must_be_finite(priority: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        _hypothesis("h_poison", priority=priority)


def test_scheduling_fields_survive_tree_round_trip() -> None:
    tree = _tree_with_path("h_old")
    tree.add_hypothesis(
        _hypothesis(
            "h_child",
            parent_id="exp_old",
            supersedes=["h_old"],
            priority=1048.0,
            order=7,
        )
    )

    loaded = ResearchTree.from_dict(tree.to_dict()).get_hypothesis("h_child")

    assert loaded.supersedes == ["h_old"]
    assert loaded.priority == 1048.0
    assert loaded.order == 7
    assert loaded.patience == 5
    assert loaded.turn_limit == 12
