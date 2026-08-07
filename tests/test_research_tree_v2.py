import json
import math
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.evaluation.types import ComparisonVerdict, EvalResult


def make_hypothesis(hypothesis_id: str) -> Hypothesis:
    return Hypothesis(
        id=hypothesis_id,
        statement=f"Hypothesis {hypothesis_id}",
        intervention=f"Apply intervention {hypothesis_id}",
        expected_effect="Improve the primary metric",
        sources=["common_knowledge"],
    )


def make_experiment(
    hypothesis_id: str,
    *,
    parent_id: str | None = None,
    kind: str = "search",
    commit: str = "a" * 40,
) -> Experiment:
    return Experiment(
        parent_id=parent_id,
        hypothesis_id=hypothesis_id,
        commit=commit,
        plan=ExperimentPlan(
            kind=kind,
            change=f"Execute {hypothesis_id}",
            run_config_ref=f"artifact://runs/{hypothesis_id}/config",
            budget={"trials": 1},
            acceptance_rule="The frozen evaluator completes",
        ),
        gitwork=GitWorkBranch(
            path=f"C:/worktrees/{hypothesis_id}",
            branch=f"experiment/{hypothesis_id}",
            base_commit=commit,
        ),
    )


def successful_eval(experiment_id: str, primary: float = 0.75) -> EvalResult:
    return EvalResult(
        experiment_id=experiment_id,
        primary=primary,
        per_sample=f"artifact://samples/{experiment_id}",
    )


def complete(tree: ResearchTree, experiment_id: str, primary: float = 0.75) -> None:
    tree.transition_experiment(experiment_id, ExperimentStatus.RUNNING)
    tree.complete_experiment(
        experiment_id,
        eval=successful_eval(experiment_id, primary),
        verdict=None,
        artifacts={
            "diff": f"artifact://diffs/{experiment_id}",
            "logs": f"artifact://logs/{experiment_id}",
        },
    )


def test_experiment_has_only_canonical_v2_fields() -> None:
    assert list(Experiment.model_fields) == [
        "parent_id",
        "hypothesis_id",
        "commit",
        "plan",
        "gitwork",
        "status",
        "eval",
        "verdict",
        "artifacts",
        "error",
    ]
    assert "id" not in Experiment.model_fields
    assert "hypothesis" not in Experiment.model_fields


def test_graph_owns_hypotheses_once_and_derives_breadth_first_children() -> None:
    tree = ResearchTree()
    for hypothesis_id in ("hyp_root", "hyp_left", "hyp_right", "hyp_leaf"):
        tree.add_hypothesis(make_hypothesis(hypothesis_id))

    tree.add_experiment("exp_root", make_experiment("hyp_root", kind="baseline"))
    tree.add_experiment("exp_left", make_experiment("hyp_left", parent_id="exp_root"))
    tree.add_experiment("exp_right", make_experiment("hyp_right", parent_id="exp_root"))
    tree.add_experiment("exp_leaf", make_experiment("hyp_leaf", parent_id="exp_left"))

    assert tree.get_hypothesis("hyp_root") == make_hypothesis("hyp_root")
    assert tree.get_experiment("exp_root").hypothesis_id == "hyp_root"
    assert tree.root_experiment_ids() == ["exp_root"]
    assert tree.list_children("exp_root") == ["exp_left", "exp_right"]
    assert tree.list_descendants("exp_root") == [
        "exp_left",
        "exp_right",
        "exp_leaf",
    ]
    assert tree.experiment_path("exp_leaf") == [
        "exp_root",
        "exp_left",
        "exp_leaf",
    ]
    assert [h.id for h in tree.hypotheses_path("exp_leaf")] == [
        "hyp_root",
        "hyp_left",
        "hyp_leaf",
    ]


def test_graph_rejects_duplicate_and_missing_references_without_mutation() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(make_hypothesis("hyp_root"))
    tree.add_experiment("exp_root", make_experiment("hyp_root", kind="baseline"))
    before = tree.to_dict()

    with pytest.raises(ValueError, match="duplicate experiment id"):
        tree.add_experiment("exp_root", make_experiment("hyp_root"))
    with pytest.raises(KeyError, match="hyp_missing"):
        tree.add_experiment("exp_missing_hyp", make_experiment("hyp_missing"))
    with pytest.raises(KeyError, match="exp_missing"):
        tree.add_experiment(
            "exp_orphan",
            make_experiment("hyp_root", parent_id="exp_missing"),
        )
    with pytest.raises(ValueError, match="duplicate hypothesis id"):
        tree.add_hypothesis(make_hypothesis("hyp_root"))

    assert tree.to_dict() == before


def test_status_transitions_are_explicit_and_terminal() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(make_hypothesis("hyp_root"))
    tree.add_experiment("exp_root", make_experiment("hyp_root", kind="baseline"))

    tree.transition_experiment("exp_root", ExperimentStatus.RUNNING)
    assert tree.get_experiment("exp_root").status is ExperimentStatus.RUNNING

    verdict = ComparisonVerdict(winner="candidate", p_value=0.01)
    tree.complete_experiment(
        "exp_root",
        eval=successful_eval("exp_root"),
        verdict=verdict,
        artifacts={"logs": "artifact://logs/exp_root"},
    )
    completed = tree.get_experiment("exp_root")
    assert completed.status is ExperimentStatus.SUCCEEDED
    assert completed.verdict == verdict
    assert completed.error is None

    with pytest.raises(ValueError, match="terminal"):
        tree.transition_experiment("exp_root", ExperimentStatus.CANCELLED)


def test_failure_and_cancellation_preserve_existing_evidence() -> None:
    tree = ResearchTree()
    for hypothesis_id in ("hyp_failed", "hyp_cancelled"):
        tree.add_hypothesis(make_hypothesis(hypothesis_id))
    tree.add_experiment("exp_failed", make_experiment("hyp_failed"))
    tree.add_experiment("exp_cancelled", make_experiment("hyp_cancelled"))

    tree.attach_artifact("exp_failed", "logs", "artifact://logs/failed")
    tree.transition_experiment("exp_failed", ExperimentStatus.RUNNING)
    tree.transition_experiment(
        "exp_failed", ExperimentStatus.FAILED, error="training failed"
    )
    tree.transition_experiment("exp_cancelled", ExperimentStatus.CANCELLED)

    failed = tree.get_experiment("exp_failed")
    assert failed.error == "training failed"
    assert failed.artifacts["logs"] == "artifact://logs/failed"
    assert tree.get_experiment("exp_cancelled").status is ExperimentStatus.CANCELLED

    with pytest.raises(ValueError, match="nonblank error"):
        pending = ResearchTree()
        pending.add_hypothesis(make_hypothesis("hyp_pending"))
        pending.add_experiment("exp_pending", make_experiment("hyp_pending"))
        pending.transition_experiment("exp_pending", ExperimentStatus.RUNNING)
        pending.transition_experiment("exp_pending", ExperimentStatus.FAILED)


def test_completion_requires_matching_finite_real_evaluation() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(make_hypothesis("hyp_root"))
    tree.add_experiment("exp_root", make_experiment("hyp_root", kind="baseline"))
    tree.transition_experiment("exp_root", ExperimentStatus.RUNNING)

    with pytest.raises(ValueError, match="experiment id"):
        tree.complete_experiment(
            "exp_root",
            eval=successful_eval("another"),
            verdict=None,
            artifacts={},
        )
    with pytest.raises(ValueError, match="finite"):
        tree.complete_experiment(
            "exp_root",
            eval=successful_eval("exp_root", math.inf),
            verdict=None,
            artifacts={},
        )

    assert tree.get_experiment("exp_root").status is ExperimentStatus.RUNNING


def test_sota_requires_eligible_successful_experiment() -> None:
    tree = ResearchTree()
    for hypothesis_id in ("hyp_baseline", "hyp_validation"):
        tree.add_hypothesis(make_hypothesis(hypothesis_id))
    tree.add_experiment(
        "exp_baseline", make_experiment("hyp_baseline", kind="baseline")
    )
    tree.add_experiment(
        "exp_validation",
        make_experiment("hyp_validation", parent_id="exp_baseline", kind="ablation"),
    )

    with pytest.raises(ValueError, match="successful"):
        tree.set_sota("exp_baseline")

    complete(tree, "exp_baseline")
    tree.set_sota("exp_baseline")
    assert tree.best_experiment_id() == "exp_baseline"

    complete(tree, "exp_validation", 0.8)
    with pytest.raises(ValueError, match="eligible"):
        tree.set_sota("exp_validation")
    assert tree.best_experiment_id() == "exp_baseline"


FIXTURE = Path("test/fixtures/research_tree_v2.json")


def fixture_payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_v2_fixture_loads_and_round_trips_with_derived_children(
    tmp_path: Path,
) -> None:
    tree = ResearchTree.load(FIXTURE)

    assert tree.best_experiment_id() == "exp_baseline"
    assert tree.list_children("exp_baseline") == ["exp_child"]
    assert "children_ids" not in json.dumps(tree.to_dict())

    target = tmp_path / "nested" / "research_tree.json"
    assert tree.save(target) == target
    assert json.loads(target.read_text(encoding="utf-8")) == fixture_payload()
    assert ResearchTree.load(target).to_dict() == tree.to_dict()


@pytest.mark.parametrize("version", [None, 1, 3])
def test_load_rejects_missing_v1_and_unknown_versions(version: int | None) -> None:
    payload = fixture_payload()
    if version is None:
        payload.pop("version")
    else:
        payload["version"] = version

    with pytest.raises(ValueError, match="unsupported research tree version"):
        ResearchTree.from_dict(payload)


def test_load_rejects_v1_nodes_shape_even_when_version_is_forged() -> None:
    with pytest.raises(ValueError, match="top-level fields"):
        ResearchTree.from_dict(
            {
                "version": 2,
                "sota_id": None,
                "nodes": {},
                "hypotheses": {},
            }
        )


def test_load_rejects_missing_parents_cycles_and_unknown_hypotheses() -> None:
    missing_parent = fixture_payload()
    missing_parent["experiments"]["exp_child"]["parent_id"] = "unknown"
    with pytest.raises(KeyError, match="unknown"):
        ResearchTree.from_dict(missing_parent)

    cycle = fixture_payload()
    cycle["experiments"]["exp_baseline"]["parent_id"] = "exp_child"
    with pytest.raises(ValueError, match="cycle"):
        ResearchTree.from_dict(cycle)

    missing_hypothesis = fixture_payload()
    missing_hypothesis["experiments"]["exp_child"]["hypothesis_id"] = "unknown"
    with pytest.raises(KeyError, match="unknown"):
        ResearchTree.from_dict(missing_hypothesis)


def test_load_rejects_invalid_records_and_ineligible_sota() -> None:
    invalid_status = fixture_payload()
    invalid_status["experiments"]["exp_child"]["status"] = "DONE"
    with pytest.raises(ValueError, match="status"):
        ResearchTree.from_dict(invalid_status)

    invalid_worktree = fixture_payload()
    invalid_worktree["experiments"]["exp_child"]["gitwork"]["path"] = ""
    with pytest.raises(ValueError, match="worktree"):
        ResearchTree.from_dict(invalid_worktree)

    invalid_artifact = fixture_payload()
    invalid_artifact["experiments"]["exp_child"]["artifacts"]["logs"] = ""
    with pytest.raises(ValueError, match="artifacts"):
        ResearchTree.from_dict(invalid_artifact)

    ineligible_sota = fixture_payload()
    ineligible_sota["experiments"]["exp_child"]["plan"]["kind"] = "ablation"
    ineligible_sota["experiments"]["exp_child"]["status"] = "SUCCEEDED"
    ineligible_sota["experiments"]["exp_child"]["eval"] = {
        "experiment_id": "exp_child",
        "primary": 0.76,
        "secondary": {},
        "per_sample": "artifact://samples/exp_child",
    }
    ineligible_sota["sota_id"] = "exp_child"
    with pytest.raises(ValueError, match="eligible"):
        ResearchTree.from_dict(ineligible_sota)


def test_load_rejects_mismatched_hypothesis_mapping_key() -> None:
    payload = fixture_payload()
    payload["hypotheses"]["hyp_baseline"]["id"] = "hyp_other"

    with pytest.raises(ValueError, match="mapping key"):
        ResearchTree.from_dict(payload)


def test_failed_atomic_replace_keeps_last_valid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "research_tree.json"
    original = '{"last":"valid"}'
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        ResearchTree().save(target)

    assert target.read_text(encoding="utf-8") == original
    assert list(tmp_path.iterdir()) == [target]
