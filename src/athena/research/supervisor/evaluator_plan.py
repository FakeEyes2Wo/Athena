"""Freeze and validate one evaluator Agent bundle."""

import csv
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from athena.core.contracts import ArtifactRef
from athena.core.workspace import resolve_workspace_path
from athena.research.contracts import EvaluatorDescriptor
from athena.research.evaluation.spec import (
    DEFAULT_PREDICTION_ID_COLUMN,
    load_evaluator_spec,
)
from athena.research.evaluation.trust import (
    extract_prediction_column,
    extract_prediction_column_from_source,
    validate_evaluator_properties,
)
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.events import wait_run_events
from athena.research.supervisor.experiment import load_agent_result
from athena.research.supervisor.plans import PlanDecision

logger = logging.getLogger(__name__)

ROW_ID_COLUMN = "__athena_row_id"


def _require_joinable_labels(
    labels_file: Path, *, id_column: str = DEFAULT_PREDICTION_ID_COLUMN
) -> None:
    """Require stable, unique row ids in one evaluator labels file."""
    with labels_file.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = [name.strip() for name in (reader.fieldnames or []) if name.strip()]
        if id_column not in columns or len(columns) < 2:
            raise ValueError(
                f"labels.csv must carry a row-id column named {id_column!r} next to "
                f"the target so predictions can be joined by id, but its header is "
                f"{columns or ['<empty>']}. Rewrite it as "
                f"'{id_column},<target>' and make evaluate.py join on that column "
                "instead of comparing the two files row by row."
            )
        seen: set[str] = set()
        for row in reader:
            raw = (row.get(id_column) or "").strip()
            if not raw:
                raise ValueError(f"labels.csv contains an empty {id_column!r}")
            if raw in seen:
                raise ValueError(f"labels.csv contains duplicate {id_column!r}: {raw}")
            seen.add(raw)


def _require_joinable_labels_dir(
    labels_dir: Path, *, id_column: str = DEFAULT_PREDICTION_ID_COLUMN
) -> None:
    """Require every labels CSV to use the stable row-id contract."""
    csv_files = sorted(labels_dir.rglob("*.csv"))
    if not csv_files:
        raise ValueError(
            f"labels/ must contain at least one .csv file carrying {ROW_ID_COLUMN!r}"
        )
    for path in csv_files:
        _require_joinable_labels(path, id_column=id_column)


def _evaluator_layout(root: Path) -> tuple[Path, str, str]:
    """Return the evaluator root, entrypoint, and prediction format."""
    spec_path = root / "metric.json"
    if not spec_path.is_file():
        existing = sorted(p.name for p in root.iterdir())[:10] if root.is_dir() else []
        raise ValueError(
            f"metric.json is missing from your workspace {root}. "
            f"That directory currently holds: {existing or 'nothing'}. "
            "Write every evaluator file inside that exact directory; files you "
            "created anywhere else do not count."
        )
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        evaluator_rel = spec["eval_script"]
    except (OSError, ValueError, KeyError, TypeError):
        # metric.json is unreadable or lacks eval_script, so freeze must reject it.
        raise ValueError("metric.json must declare eval_script")
    prediction_format = spec.get("prediction_format", "tabular_csv")
    try:
        evaluator_path = resolve_workspace_path(root, evaluator_rel)
    except ValueError as exc:
        # The declared evaluator path escapes its frozen workspace.
        raise ValueError(f"output path escapes workspace: {evaluator_rel}") from exc
    if evaluator_path.is_dir():
        evaluator_root = evaluator_path
        entrypoint = "evaluate.py"
        if not (evaluator_root / entrypoint).is_file():
            raise ValueError(
                "eval_script directory must contain an entrypoint file "
                f"named evaluate.py: {evaluator_rel!r}"
            )
    elif evaluator_path.is_file():
        evaluator_root = evaluator_path.parent
        entrypoint = evaluator_path.name
    else:
        raise ValueError("eval_script is missing")
    return evaluator_root, entrypoint, prediction_format


def _evaluator_readme(root: Path, *, entrypoint: str, prediction_format: str) -> str:
    """Build the freeze marker for an accepted evaluator."""
    spec = None
    metric_path = root / "metric.json"
    if metric_path.is_file():
        spec = load_evaluator_spec(root)
    contract_lines = ""
    if spec is not None:
        probabilities = ", ".join(spec.probability_columns) or "none"
        contract_lines = (
            "\n## Public prediction contract\n\n"
            f"- task_id: `{spec.task_id}`\n"
            f"- prediction_file: `{spec.prediction_file}`\n"
            f"- prediction_id_column: `{spec.prediction_id_column}`\n"
            f"- prediction_column: `{spec.prediction_column}`\n"
            f"- probability_columns: `{probabilities}`\n"
            f"- metrics_file: `{spec.metrics_file}`\n"
        )
    return (
        "# Evaluator Freeze Marker\n\n"
        f"This evaluator in `{root}` has been accepted.\n\n"
        "## Freeze contract\n\n"
        f"- Do NOT modify `{entrypoint}`, `metric.json`, labels, `HANDOFF.md`, "
        "`pyproject.toml`, or `README.md` further.\n"
        "- This directory is the authoritative evaluator for the current run.\n"
        "- If a change is required, create a new evaluator version and rerun the "
        "full acceptance flow.\n\n"
        "## Declared format\n\n"
        f"- entrypoint: `{entrypoint}`\n"
        f"- prediction_format: `{prediction_format}`\n"
        "- See `HANDOFF.md` for the precise prediction schema, identity key, "
        "held-out split, and metric definition.\n" + contract_lines
    )


def _reject_bundled_predictions(evaluator_root: Path, prediction_file: str) -> None:
    """Refuse a bundle that ships its own copy of the scored artifact.

    The platform writes ``predictions/<prediction_file>`` before every scoring
    run. A same-named file left beside the metric code shadows it for any script
    that reads the bare name, and then every score is that stale file's score.
    On 2026-09-06 the sensitivity probe answered "permuting values did not
    change the score" for eight consecutive turns while the evaluator kept
    scoring the sample predictions its own agent had written; the run spent its
    whole evaluator budget and PREPARE failed.
    """
    stray = sorted(
        path.name
        for path in evaluator_root.glob("*.csv")
        if path.name == prediction_file or path.name.startswith("predictions__")
    )
    if stray:
        raise ValueError(
            "the evaluator bundle must not contain a prediction file: "
            + ", ".join(stray)
            + f". The platform writes predictions/{prediction_file} before each "
            "scoring run, and a copy beside the metric code shadows it. Delete "
            f"these files and read predictions/{prediction_file}."
        )


async def _validate_frozen_evaluator(
    *,
    root: Path,
    scripts: DataScriptRunner,
) -> None:
    """Run format-aware property tests on a frozen evaluator directory."""
    evaluator_root, entrypoint, prediction_format = _evaluator_layout(root)
    spec = (
        load_evaluator_spec(evaluator_root)
        if (evaluator_root / "metric.json").is_file()
        else None
    )
    id_column = spec.prediction_id_column if spec is not None else ROW_ID_COLUMN
    prediction_column_from_spec = spec.prediction_column if spec is not None else None
    if spec is not None:
        required = ("metric.json", entrypoint, "HANDOFF.md", "pyproject.toml")
        missing = [name for name in required if not (evaluator_root / name).is_file()]
        if missing:
            raise ValueError(
                "new evaluator bundle is missing required files: " + ", ".join(missing)
            )
        if (
            not (evaluator_root / "labels.csv").is_file()
            and not (evaluator_root / "labels").is_dir()
        ):
            raise ValueError("new evaluator bundle must contain labels.csv or labels/")
    if prediction_format != "tabular_csv":
        logger.warning(
            "evaluator property tests skipped: prediction_format=%s",
            prediction_format,
        )
        return
    labels_file = evaluator_root / "labels.csv"
    labels_dir = evaluator_root / "labels"
    if labels_file.is_file():
        _require_joinable_labels(labels_file, id_column=id_column)
        labels_csv = labels_file.read_text(encoding="utf-8-sig")
    elif labels_dir.is_dir():
        _require_joinable_labels_dir(labels_dir, id_column=id_column)
        csv_files = sorted(labels_dir.rglob("*.csv"))
        if not csv_files:
            raise ValueError("labels/ has no csv for evaluator property tests")
        labels_csv = csv_files[0].read_text(encoding="utf-8-sig")
    else:
        raise ValueError("no labels found for evaluator property tests")

    # Read the declared prediction column, then fall back to evaluator source.
    handoff_path = evaluator_root / "HANDOFF.md"
    handoff_text = (
        handoff_path.read_text(encoding="utf-8") if handoff_path.is_file() else ""
    )
    prediction_column = extract_prediction_column(handoff_text)
    if prediction_column is None:
        prediction_column = prediction_column_from_spec
    if prediction_column is None:
        source_path = evaluator_root / entrypoint
        source = (
            source_path.read_text(encoding="utf-8", errors="replace")
            if source_path.is_file()
            else ""
        )
        prediction_column = extract_prediction_column_from_source(source)
    if prediction_column is None:
        raise ValueError(
            "cannot determine the tabular prediction CSV column from either "
            "HANDOFF.md or the evaluator source; declare it in HANDOFF.md or "
            "make evaluate.py read a clearly named prediction column"
        )

    prediction_file = spec.prediction_file if spec is not None else "predictions.csv"
    _reject_bundled_predictions(evaluator_root, prediction_file)

    async def score(predictions_csv: str) -> float:
        """Run the frozen evaluator against one synthetic prediction CSV."""
        result = await scripts.run_dir(
            evaluator_root,
            request={},
            extra_files={
                f"predictions/{prediction_file}": predictions_csv.encode("utf-8")
            },
            output_schema={"primary": None},
        )
        return float(result.outputs["primary"])

    outcome = await validate_evaluator_properties(
        labels_csv,
        score,
        prediction_column=prediction_column,
        prediction_id_column=id_column,
        probability_columns=spec.probability_columns if spec is not None else (),
    )
    if not outcome.get("ok"):
        # Name the file the probe wrote: a script that reads some other path
        # sees a constant score, and the bare property name reads as a metric
        # problem rather than a path problem.
        raise ValueError(
            f"{outcome.get('reason', 'evaluator property tests failed')}. The "
            f"platform wrote predictions/{prediction_file} before running "
            f"{entrypoint}; score exactly that file."
        )


async def run_evaluator_plan(
    runtime: Any,
    evaluator_dir: Path,
    task: str,
    plan_id: str,
    max_turns: int,
) -> ArtifactRef:
    """Run and repair one evaluator Agent until its bundle is accepted."""
    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    root = Path(evaluator_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    runtime.execution.ensure_environment()
    context_ref = await runtime.store.put_text(
        json.dumps({"plan_id": plan_id, "task": task}, ensure_ascii=False)
    )
    agent_id, run_id = await runtime.agents.create_root(
        "evaluator",
        {"content": task, "context_refs": [context_ref]},
        agent_id=plan_id,
        name=plan_id,
    )

    # Iterate Agent repairs until one submitted evaluator passes every freeze check.
    feedback: str | None = None
    try:
        for turn in range(max_turns):
            if turn:
                run_id = await runtime.agents.followup(
                    agent_id,
                    {"content": feedback, "context_refs": []},
                )
            summary = await wait_run_events(
                runtime.agents,
                run_id,
                lambda kind, ref, data: runtime.events.project_agent_event(
                    plan_id, kind, ref, data
                ),
            )
            try:
                decision = await load_agent_result(summary, runtime.store, PlanDecision)
                if decision is None:
                    raise RuntimeError(summary.error or "evaluator Agent run failed")
            except (OSError, RuntimeError, ValueError) as exc:
                # Invalid Agent output becomes bounded repair feedback.
                feedback = (
                    "previous evaluator turn did not produce a valid decision: "
                    f"{' '.join(str(exc).split())[:700]}. "
                    "Do not run tools again. Reply only with compact PlanDecision "
                    "JSON; keep reason under 120 characters."
                )
                continue
            if decision.decision == "abandon":
                raise RuntimeError(f"evaluator Agent abandoned Plan: {decision.reason}")
            if decision.decision != "submit":
                feedback = (
                    f"Return submit to advance; got {decision.decision!r}. "
                    "Continue means keep repairing the evaluator, not accept it."
                )
                continue
            try:
                _, entrypoint, prediction_format = _evaluator_layout(root)
                await _validate_frozen_evaluator(
                    root=root,
                    scripts=runtime.scripts,
                )
                readme_text = _evaluator_readme(
                    root,
                    entrypoint=entrypoint,
                    prediction_format=prediction_format,
                )
                (root / "README.md").write_text(readme_text, encoding="utf-8")
                readme_ref = await runtime.store.put_text(readme_text)
                evaluator_ref = await runtime.store.put_text(
                    EvaluatorDescriptor(
                        dir_path=str(root.resolve()),
                        readme_ref=readme_ref,
                        prediction_format=prediction_format,
                        entrypoint=entrypoint,
                    ).model_dump_json()
                )
                return evaluator_ref
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                # Freeze validation failed, so return concise feedback to this Plan.
                feedback = " ".join(str(exc).split())[:1000]
                logger.warning(
                    "evaluator %s submit rejected on turn %d/%d: %s",
                    plan_id,
                    turn + 1,
                    max_turns,
                    feedback,
                )
        raise RuntimeError(
            "evaluator turn budget exhausted without an accepted evaluator "
            f"({plan_id}); last rejection: {feedback or 'none recorded'}"
        )
    finally:
        await runtime.agents.reap(agent_id)
