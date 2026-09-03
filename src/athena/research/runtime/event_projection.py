"""Pure projections from Supervisor data to runtime event payloads."""

from typing import Any

from athena.research.supervisor.events import EventProjector, StateEvent
from athena.research.supervisor.scheduling import count_search_attempts


def supervisor_output(
    projector: EventProjector, payload: dict[str, object]
) -> dict[str, object]:
    """Project one raw Supervisor output dictionary into an output event."""
    event = projector.output(
        source=payload.get("source", "supervisor"),
        channel=payload.get("channel", "text"),
        text=str(payload.get("text", "")),
        plan=payload.get("plan"),
        tool=payload.get("tool"),
        artifact_ref=payload.get("artifact_ref"),
        truncated=payload.get("truncated", False),
        message_id=payload.get("message_id"),
        session_id=payload.get("session_id"),
        scope=payload.get("scope"),
        scope_id=payload.get("scope_id"),
    )
    return event.model_dump(mode="json")


def supervisor_state(supervisor: Any, ideator_lanes: int) -> StateEvent:
    """Build a replaceable runtime snapshot from Supervisor-owned state."""
    state = supervisor.state
    tree = supervisor.tree
    plans = [
        {"id": plan_id, **plan.model_dump(mode="json")}
        for plan_id, plan in state.plans.items()
    ]
    successes = sum(
        experiment.status.value == "SUCCEEDED"
        for experiment in tree.experiments(kind="search")
    )
    sota_id = tree.best_experiment_id()
    sota = None
    if sota_id is not None:
        experiment = tree.get_experiment(sota_id)
        sota = {
            "experiment": sota_id,
            "metric": experiment.eval.primary if experiment.eval else None,
            "commit": experiment.commit,
        }
    waiting_ids = [
        plan_id
        for plan_id, plan in state.plans.items()
        if plan.turn_limit is not None and plan.turns_used >= plan.turn_limit
    ]
    pending = [
        {"id": hypothesis.id, "statement": hypothesis.statement}
        for hypothesis in tree.pending_hypotheses()
        if hypothesis.id is not None
    ]
    return StateEvent(
        status=state.status,
        phase=state.phase,
        plans=plans,
        search={
            "attempts": count_search_attempts(state, tree),
            "limit": state.search_limit,
            "successes": successes,
            "concurrency": state.concurrency,
            "ideator_lanes": ideator_lanes,
        },
        sota=sota,
        waiting=(
            {"plans": waiting_ids, "reason": "turn_limit_exhausted"}
            if waiting_ids
            else None
        ),
        manual=state.manual_mode,
        pending=pending,
        validation=state.validation,
        eda_dir=state.eda_dir,
        task_understanding=state.task_understanding,
    )


__all__ = ["supervisor_output", "supervisor_state"]
