"""Rendering and atomic materialization of the confirmed task handoff."""

import os
from pathlib import Path

from athena.research.clarification.errors import ClarificationPersistenceError
from athena.research.clarification.models import ClarificationDraft, DraftUnderstanding


def render_handoff(draft: ClarificationDraft) -> str:
    """Render the immutable human-readable confirmed task contract."""
    lines = [
        "# TASK_CLARIFICATION",
        "",
        f"- Draft ID: {draft.draft_id}",
        f"- Revision: {draft.revision}",
        f"- Session ID: {draft.session_id}",
        f"- Status: {draft.status}",
        "",
        "## Original task",
        draft.original_task or "(none)",
        "",
        "## Questions and outcomes",
    ]
    if not draft.answers:
        lines.append("(no clarification questions were asked)")
    for index, answer in enumerate(draft.answers, start=1):
        lines.extend(
            [f"{index}. Q: {answer.question}", f"   Outcome: {answer.outcome}"]
        )
        if answer.value:
            lines.append(f"   Value: {answer.value}")
        if answer.choice_label:
            lines.append(f"   Choice label: {answer.choice_label}")
        if answer.outcome in {"timeout", "cancelled"}:
            lines.append(f"   Settled at: {answer.answered_at.isoformat()}")
    lines.extend(
        ["", "## Confirmed understanding", _understanding(draft.understanding)]
    )
    lines.extend(["", "## Unresolved items"])
    if not draft.unresolved:
        lines.append("- (none)")
    for item in draft.unresolved:
        level = "critical" if item.critical else "optional"
        lines.append(f"- [{level}] {item.field}: {item.reason}")
    return "\n".join(lines)


def materialize_handoff(path: Path, draft: ClarificationDraft) -> str:
    """Atomically write the named handoff and return its exact content."""
    text = render_handoff(draft)
    atomic_write_text(path, text)
    return text


def atomic_write_text(path: Path, text: str) -> None:
    """Replace a UTF-8 text file without exposing partial content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        # The destination cannot be replaced, so remove the incomplete temp file.
        temporary.unlink(missing_ok=True)
        raise ClarificationPersistenceError(f"handoff_write_failed: {error}") from error


def _understanding(value: DraftUnderstanding) -> str:
    return "\n".join(
        [
            f"- Title: {value.title or ''}",
            f"- Dataset: {value.dataset or '(unknown)'}",
            f"- Target: {value.target or '(unknown)'}",
            f"- Task type: {value.task_type}",
            f"- Primary metric: {value.primary_metric or '(unknown)'}",
            f"- Direction: {value.direction or '(unknown)'}",
            f"- Evaluation plan: {value.evaluation_plan or '(unknown)'}",
        ]
    )


__all__ = ["atomic_write_text", "materialize_handoff", "render_handoff"]
