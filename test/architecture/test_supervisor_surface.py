"""Architecture boundary for the autonomous Supervisor runtime."""

from pathlib import Path

SUPERVISOR_ROOT = Path("src/athena/research/supervisor")


def _python_module_names(root: Path) -> set[str]:
    return {path.stem for path in root.glob("*.py")}


def test_supervisor_has_only_coarse_plan_modules() -> None:
    assert _python_module_names(SUPERVISOR_ROOT) == {
        "__init__",
        "deps",
        "evaluator_plan",
        "events",
        "experiment",
        "manifest",
        "phases",
        "plan_lifecycle",
        "plan_runtime",
        "plans",
        "prepare",
        "prompt_context",
        "recovery",
        "run_state",
        "scheduling",
        "search_loop",
        "settlement",
        "state",
        "statistics",
        "supervisor",
        "validation",
        "validation_contracts",
    }
    for retired in ("policy", "ranker", "scheduler"):
        assert not (SUPERVISOR_ROOT / f"{retired}.py").exists()
    assert not (SUPERVISOR_ROOT / "planning").exists()
    assert not (SUPERVISOR_ROOT / "storage").exists()


def test_active_surfaces_have_no_legacy_protocol_or_session_reader() -> None:
    sources = [
        Path("src/athena/research/runtime/facade.py"),
        Path("src/athena/cli.py"),
        *Path("src/athena_tui").glob("*.py"),
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    for forbidden in (
        "PlanJournal",
        "SupervisorCoordinator",
        "ProjectStateStore",
        "HumanRequest",
        "REQUESTS_GET",
        "HUMAN_REPLY",
        "MESSAGES_GET",
        "read_new_messages",
        ".athena/sessions",
    ):
        assert forbidden not in text
