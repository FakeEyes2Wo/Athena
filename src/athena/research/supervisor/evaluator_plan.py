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
    EvaluatorSpec,
    parse_evaluator_spec,
)
from athena.research.evaluation.trust import (
    extract_label_column_from_source,
    extract_prediction_column,
    extract_prediction_column_from_source,
    validate_evaluator_properties,
)
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.events import wait_run_events
from athena.research.supervisor.experiment import load_agent_result
from athena.research.supervisor.plans import PlanDecision, plan_budget_note

logger = logging.getLogger(__name__)

ROW_ID_COLUMN = "__athena_row_id"


def _resolve_prediction_column(
    declared: str | None, evaluator_root: Path, entrypoint: str
) -> str:
    """Resolve the synthetic probe field, preferring metric.json's contract."""
    if declared:
        return declared
    handoff_path = evaluator_root / "HANDOFF.md"
    handoff_text = (
        handoff_path.read_text(encoding="utf-8") if handoff_path.is_file() else ""
    )
    from_handoff = extract_prediction_column(handoff_text)
    if from_handoff is not None:
        return from_handoff
    source_path = evaluator_root / entrypoint
    source = (
        source_path.read_text(encoding="utf-8", errors="replace")
        if source_path.is_file()
        else ""
    )
    from_source = extract_prediction_column_from_source(source)
    if from_source is not None:
        return from_source
    raise ValueError(
        "cannot determine the tabular prediction CSV column from the declared "
        "metric contract, HANDOFF.md, or the evaluator source; declare "
        "prediction_column in metric.json (or in HANDOFF.md for a legacy "
        "descriptor) so the property probe can build a synthetic prediction file"
    )


def _resolve_label_column(
    declared: str | None, evaluator_root: Path, entrypoint: str
) -> str | None:
    """Resolve optional labels target declaration without guessing by position."""
    if declared:
        return declared
    source_path = evaluator_root / entrypoint
    source = (
        source_path.read_text(encoding="utf-8", errors="replace")
        if source_path.is_file()
        else ""
    )
    return extract_label_column_from_source(source)


def _require_joinable_labels(
    labels_file: Path, *, id_column: str = DEFAULT_PREDICTION_ID_COLUMN
) -> None:
    """Require stable, unique row ids in one evaluator labels file."""
    with labels_file.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        raw_columns = reader.fieldnames or []
        columns = [name.strip() for name in raw_columns]
        if any(not name for name in columns) or len(set(columns)) != len(columns):
            raise ValueError("labels.csv contains empty or duplicate column names")
        reader.fieldnames = columns
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


def _csv_identity_values(path: Path, id_column: str) -> set[str]:
    """Read normalized, non-empty row identities without retaining them in errors."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        raw_fields = reader.fieldnames or []
        fields = [field.strip() for field in raw_fields]
        if (
            not fields
            or any(not field for field in fields)
            or len(set(fields)) != len(fields)
        ):
            raise ValueError(f"{path.name} has empty or duplicate CSV columns")
        reader.fieldnames = fields
        if id_column not in fields:
            raise ValueError(
                f"{path.name} is missing declared identity column {id_column!r}"
            )
        values: set[str] = set()
        count = 0
        for row in reader:
            if None in row or None in row.values():
                raise ValueError(
                    f"{path.name} contains a row with an invalid field count"
                )
            value = (row.get(id_column) or "").strip()
            if not value:
                raise ValueError(f"{path.name} contains an empty identity value")
            if value in values:
                raise ValueError(f"{path.name} contains duplicate identity values")
            values.add(value)
            count += 1
        if count == 0:
            raise ValueError(f"{path.name} contains no data rows")
        return values


def _require_label_coverage(
    root: Path, expected_features: Path, *, id_column: str
) -> None:
    """Require frozen evaluator labels to cover exactly its own feature split."""
    labels_file = root / "labels.csv"
    label_files = (
        [labels_file]
        if labels_file.is_file()
        else sorted((root / "labels").rglob("*.csv"))
    )
    expected_ids = _csv_identity_values(expected_features, id_column)
    actual_ids: set[str] = set()
    for path in label_files:
        current = _csv_identity_values(path, id_column)
        if actual_ids.intersection(current):
            raise ValueError(
                "evaluator labels contain duplicate identity values across files"
            )
        actual_ids.update(current)
    missing = len(expected_ids - actual_ids)
    extra = len(actual_ids - expected_ids)
    if missing or extra:
        raise ValueError(
            "evaluator labels do not exactly cover this split's feature rows "
            f"(missing {missing}, unexpected {extra}); rebuild labels from the "
            "matching SEARCH or FINAL split only"
        )


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


def _evaluator_layout(
    root: Path,
) -> tuple[Path, str, str, EvaluatorSpec | None]:
    """Read and validate the evaluator contract, then resolve its entrypoint."""
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
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Invalid JSON or an unreadable contract cannot be frozen.
        raise ValueError("metric.json must declare eval_script") from exc
    if not isinstance(payload, dict):
        raise TypeError("metric.json must contain a JSON object")
    try:
        evaluator_rel = payload["eval_script"]
    except KeyError as exc:
        # Freeze requires an explicit entrypoint, including for v2 contracts.
        raise ValueError("metric.json must declare eval_script") from exc
    if not isinstance(evaluator_rel, str):
        raise TypeError("metric.json eval_script must be a string")
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
    spec = parse_evaluator_spec(payload)
    prediction_format = (
        spec.prediction_format
        if spec is not None
        else payload.get("prediction_format", "tabular_csv")
    )
    return evaluator_root, entrypoint, prediction_format, spec


def _evaluator_readme(
    root: Path,
    *,
    entrypoint: str,
    prediction_format: str,
    spec: EvaluatorSpec | None,
) -> str:
    """Build the freeze marker for an accepted evaluator."""
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


def _resolve_prediction_column(
    declared: str | None, evaluator_root: Path, entrypoint: str
) -> str:
    """Return the prediction CSV column the property probe must synthesise.

    The declared contract wins. ``metric.json`` is machine-readable and is what
    the evaluator itself reads back, so it is authoritative; the prose and
    source fallbacks exist only for legacy descriptors that declare no column.

    Deriving the column from HANDOFF.md first let an arbitrary word in the
    handoff prose override the declared name. The probe then wrote a prediction
    file the evaluator rejected, every scoring call returned the evaluator's
    error sentinel, and the permutation check saw an unchanged score — so a
    correct evaluator was rejected and the run died in PREPARE.
    """
    if declared:
        return declared
    handoff_path = evaluator_root / "HANDOFF.md"
    handoff_text = (
        handoff_path.read_text(encoding="utf-8") if handoff_path.is_file() else ""
    )
    from_handoff = extract_prediction_column(handoff_text)
    if from_handoff is not None:
        return from_handoff
    source_path = evaluator_root / entrypoint
    source = (
        source_path.read_text(encoding="utf-8", errors="replace")
        if source_path.is_file()
        else ""
    )
    from_source = extract_prediction_column_from_source(source)
    if from_source is not None:
        return from_source
    raise ValueError(
        "cannot determine the tabular prediction CSV column from the declared "
        "metric contract, HANDOFF.md, or the evaluator source; declare "
        "prediction_column in metric.json (or in HANDOFF.md for a legacy "
        "descriptor) so the property probe can build a synthetic prediction file"
    )


async def _validate_frozen_evaluator(
    *,
    root: Path,
    scripts: DataScriptRunner,
    layout: tuple[Path, str, str, EvaluatorSpec | None],
    expected_features: Path | None = None,
) -> None:
    """Run format-aware property tests on a frozen evaluator directory."""
    evaluator_root, entrypoint, prediction_format, spec = layout
    id_column = spec.prediction_id_column if spec is not None else ROW_ID_COLUMN
    prediction_column_from_spec = spec.prediction_column if spec is not None else None
    if spec is not None:
        required = {
            "metric.json": root / "metric.json",
            entrypoint: evaluator_root / entrypoint,
            "HANDOFF.md": evaluator_root / "HANDOFF.md",
            "pyproject.toml": evaluator_root / "pyproject.toml",
        }
        missing = [name for name, path in required.items() if not path.is_file()]
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

    if expected_features is not None:
        _require_label_coverage(evaluator_root, expected_features, id_column=id_column)

    # Resolve declarations before the legacy prose/source fallbacks.
    prediction_column = _resolve_prediction_column(
        prediction_column_from_spec, evaluator_root, entrypoint
    )
    label_column = _resolve_label_column(
        spec.label_column if spec is not None else None, evaluator_root, entrypoint
    )

    prediction_file = spec.prediction_file if spec is not None else "predictions.csv"

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
        label_column=label_column,
        probability_columns=spec.probability_columns if spec is not None else (),
    )
    if not outcome.get("ok"):
        raise ValueError(outcome.get("reason", "evaluator property tests failed"))


async def run_evaluator_plan(
    runtime: Any,
    evaluator_dir: Path,
    task: str,
    plan_id: str,
    max_turns: int,
    expected_features: Path | None = None,
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
                    "Continue means keep repairing the evaluator, not accept it. "
                    + plan_budget_note(turn=turn, max_turns=max_turns)
                )
                continue
            try:
                layout = _evaluator_layout(root)
                _evaluator_root, entrypoint, prediction_format, spec = layout
                await _validate_frozen_evaluator(
                    root=root,
                    scripts=runtime.scripts,
                    expected_features=expected_features,
                    layout=layout,
                )
                readme_text = _evaluator_readme(
                    root,
                    entrypoint=entrypoint,
                    prediction_format=prediction_format,
                    spec=spec,
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
            except (OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
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
