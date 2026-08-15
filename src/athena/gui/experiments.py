"""Experiment queries and management over the research tree."""

from typing import Any

from athena.core.research_tree import ExperimentStatus, ResearchTree


def _detail(tree: ResearchTree, exp_id: str) -> dict[str, Any]:
    """Expand one experiment into an id-stamped detail with its hypothesis."""
    data = tree.to_dict()
    experiment = dict(data["experiments"][exp_id])
    experiment["id"] = exp_id
    hyp_id = experiment["hypothesis_id"]
    hypothesis = dict(data["hypotheses"][hyp_id])
    hypothesis["id"] = hyp_id
    return {
        "experiment": experiment,
        "hypothesis": hypothesis,
        "sota": exp_id == data.get("sota_id"),
    }


def list_experiments(tree: ResearchTree, kind: str | None = None) -> list[dict[str, Any]]:
    """List experiments, optionally filtered to one plan kind, ordered by hypothesis order."""
    data = tree.to_dict()
    details: list[dict[str, Any]] = []
    for exp_id in data["experiments"]:
        if kind is not None and data["experiments"][exp_id]["plan"]["kind"] != kind:
            continue
        details.append(_detail(tree, exp_id))

    def sort_key(detail: dict[str, Any]) -> tuple[bool, int]:
        order = detail["hypothesis"].get("order")
        return (order is None, order if order is not None else 0)

    details.sort(key=sort_key)
    return details


def get_experiment_detail(tree: ResearchTree, exp_id: str) -> dict[str, Any]:
    """Return one experiment detail plus its ancestor path and descendants."""
    detail = _detail(tree, exp_id)
    detail["path"] = tree.experiment_path(exp_id)
    detail["descendants"] = tree.list_descendants(exp_id)
    return detail


def transition(
    tree: ResearchTree, exp_id: str, status: str, error: str | None = None
) -> dict[str, Any]:
    """Advance an experiment status via the allowed-transition table."""
    tree.transition_experiment(exp_id, ExperimentStatus(status), error=error)
    return _detail(tree, exp_id)


def set_sota(tree: ResearchTree, exp_id: str) -> dict[str, Any]:
    """Mark a successful baseline/search experiment as SOTA."""
    tree.set_sota(exp_id)
    return {"sota_id": tree.best_experiment_id()}
