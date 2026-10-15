"""Experiment documents are isolated by ResearchRuntime session state root."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.core.research_tree import ResearchTree
from athena.research.config import SessionConfig
from athena.research.experiment_documents import ProjectionContext
from athena.research.runtime import ResearchRuntime


def _baseline_event(score: float, commit: str) -> dict[str, object]:
    return {
        "run_id": "exp_baseline",
        "stage": "baseline",
        "status": "SUCCEEDED",
        "metric": {
            "primary": score,
            "reference": None,
            "generalization_gap": None,
            "secondary": {},
        },
        "artifacts": {},
        "reason": {"kind": "trusted_score", "summary": "Baseline completed."},
        "provenance": {
            "phase": "PREPARE",
            "experiment_id": "exp_baseline",
            "commit": commit,
        },
    }


def _context() -> ProjectionContext:
    return ProjectionContext(
        tree=ResearchTree(),
        state=SimpleNamespace(
            validation=None,
            validation_skipped=None,
            task_understanding=None,
        ),
        direction="maximize",
    )


def _documents(runtime: ResearchRuntime):
    return runtime.supervisor._deps.runtime.documents


@pytest.mark.asyncio
async def test_named_session_baseline_archives_are_isolated_and_immutable(
    tmp_path: Path,
) -> None:
    legacy = ResearchRuntime(project_root=tmp_path)
    session_a = ResearchRuntime(
        project_root=tmp_path,
        session=SessionConfig(
            state_root=tmp_path / ".athena" / "conversations" / "s-a",
            session_id="s-a",
        ),
    )
    session_b = ResearchRuntime(
        project_root=tmp_path,
        session=SessionConfig(
            state_root=tmp_path / ".athena" / "conversations" / "s-b",
            session_id="s-b",
        ),
    )

    try:
        assert _documents(legacy).project(_baseline_event(0.5, "legacy"), _context())
        assert _documents(session_a).project(
            _baseline_event(0.6, "session-a"), _context()
        )
        assert _documents(session_b).project(
            _baseline_event(0.8, "session-b"), _context()
        )

        legacy_root = tmp_path / ".athena" / "exp_docs"
        session_a_root = tmp_path / ".athena" / "conversations" / "s-a" / "exp_docs"
        session_b_root = tmp_path / ".athena" / "conversations" / "s-b" / "exp_docs"
        legacy_archive = (legacy_root / "runs" / "exp_baseline.json").read_bytes()
        session_a_archive = session_a_root / "runs" / "exp_baseline.json"
        session_a_before_conflict = session_a_archive.read_bytes()

        assert json.loads(legacy_archive)["provenance"]["commit"] == "legacy"
        assert (
            json.loads(session_a_archive.read_bytes())["provenance"]["commit"]
            == "session-a"
        )
        assert (
            json.loads((session_b_root / "runs" / "exp_baseline.json").read_bytes())[
                "provenance"
            ]["commit"]
            == "session-b"
        )
        assert (
            json.loads((session_a_root / "baseline.json").read_bytes())["metric"][
                "primary"
            ]
            == 0.6
        )
        assert (
            json.loads((session_b_root / "baseline.json").read_bytes())["metric"][
                "primary"
            ]
            == 0.8
        )

        # Same-session run IDs remain immutable and reject changed bytes.
        assert not _documents(session_a).project(
            _baseline_event(0.9, "different-commit"), _context()
        )
        assert session_a_archive.read_bytes() == session_a_before_conflict
        assert (
            json.loads(session_a_archive.read_bytes())["provenance"]["commit"]
            == "session-a"
        )
    finally:
        await legacy.aclose()
        await session_a.aclose()
        await session_b.aclose()
