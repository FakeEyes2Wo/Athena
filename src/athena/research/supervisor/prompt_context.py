"""Model-visible context blocks for research agents."""

from athena.research.supervisor.events import bounded_redacted_tail


def _prompt_block(title: str, body: str, end_marker: str) -> str:
    body = body.strip()
    if not body:
        return ""
    return f"\n\n--- {title} ---\n{body}\n{end_marker}"


def handoff_block(handoff: str) -> str:
    """Build evaluator contract context for model prompts."""
    return _prompt_block(
        "Evaluator contract (authoritative; your predictions must match it exactly)",
        handoff,
        "--- end of evaluator contract ---",
    )


def hypothesis_block(statement: str, intervention: str, expected: str) -> str:
    """Build hypothesis prompt block for one SEARCH/implementation attempt."""
    details = [
        ("Claim", statement),
        ("Intervention to implement", intervention),
        ("Expected effect", expected),
    ]
    body = "\n".join(
        f"{label}: {value.strip()}"
        for label, value in details
        if value and value.strip()
    )
    return _prompt_block(
        "Hypothesis under test (implement exactly this, nothing else)",
        body,
        "--- end of hypothesis ---",
    )


def trusted_score_block(
    *,
    reference_metric: float | None,
    direction: str,
    tolerance: float,
    best_metric: float | None,
    best_commit: str | None,
    stale_rounds: int,
) -> str:
    """Render the platform-scored reference and the Plan's trusted best."""
    if reference_metric is None and best_metric is None:
        return ""
    lines = [
        (
            "Trusted SEARCH scores (from Athena's frozen evaluator; local OOF/CV "
            "scores are diagnostics and are not trusted SEARCH results):"
        ),
        (
            "After each continue, Athena automatically executes the manifest, scores "
            "valid predictions, and commits a scored revision before this next turn. "
            "A saved best below has already been executed and scored; it is not "
            "waiting for you to run it again. Read EXPERIMENT_LOG.md and git status "
            "to distinguish the scored commit from any new unscored edits."
        ),
    ]
    if reference_metric is not None:
        lines.append(
            f"- Frozen reference metric: {reference_metric:.8f} "
            f"(direction={direction}, tolerance={tolerance:.8f})."
        )
    if best_metric is None:
        lines.append(
            "- Historical trusted best: none yet. A submit still runs the current "
            "proposal; execution/scoring failures remain repairable in this Plan."
        )
    else:
        lines.append(
            f"- Historical best trusted SEARCH metric: {best_metric:.8f}; "
            f"scored commit: {best_commit or 'unknown'}; stale scored rounds: "
            f"{stale_rounds}."
        )
        lines.append(
            "- With an existing best, submit settles only that saved, trusted commit; "
            "current unscored edits cannot become the best by submission. Use "
            "continue to have the current edits executed and scored."
        )
    return _prompt_block(
        "Trusted score state", "\n".join(lines), "--- end of trusted score state ---"
    )


def data_contract_block(contract: str) -> str:
    """Build mandatory data-contract block for plan input prompts."""
    return _prompt_block(
        "Data contract (violating this invalidates your score)",
        contract,
        "--- end of data contract ---",
    )


def failure_block(kind: str, error: str) -> str:
    """Build previous-turn failure block shown to the model."""
    detail = bounded_redacted_tail(error, 800)
    if not detail:
        body = ""
    else:
        body = (
            f"Failure kind: {kind}\n"
            f"Detail: {detail}\n"
            "Do not resubmit the same experiment unchanged. If you continue trying a "
            "new candidate, use tools in this turn to make a concrete change that "
            "addresses this failure and execute it; a plan or promise to patch it in "
            "a later turn does not count as a repair. If a trusted historical best "
            "already exists, submit settles that saved result; unscored edits cannot "
            "replace it. If you cannot make a repair and there is no trusted result "
            "to submit, explain the blocker and abandon."
        )
    return _prompt_block(
        "Previous attempt failed (repair a new candidate or submit a saved best)",
        body,
        "--- end of failure report ---",
    )
