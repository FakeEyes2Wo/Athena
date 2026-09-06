"""Model-visible context blocks for research agents."""


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


def data_contract_block(contract: str) -> str:
    """Build mandatory data-contract block for plan input prompts."""
    return _prompt_block(
        "Data contract (violating this invalidates your score)",
        contract,
        "--- end of data contract ---",
    )


def failure_block(kind: str, error: str) -> str:
    """Build previous-turn failure block shown to the model."""
    detail = " ".join(error.split())[:800]
    if not detail:
        body = ""
    else:
        body = (
            f"Failure kind: {kind}\n"
            f"Detail: {detail}\n"
            "Do not resubmit the same experiment unchanged; diagnose this failure first, "
            "then retry."
        )
    return _prompt_block(
        "Previous attempt failed (fix this before anything else)",
        body,
        "--- end of failure report ---",
    )
