import pytest
import pandas as pd
from athena.core.workspace import GitWorkBranch
from athena.core.research_tree import ExperimentStatus, ResearchTree
from athena.evaluation.types import EvalResult, EvalSpec, MetricDef
from athena.research.models import MetricSpec, TaskMetaData
from athena.data.types import DataProfile, ProcessingLog
from athena.experiment import pipeline
from athena.workflows.prepare.baseline import BaselineDraft, create_baseline
from athena.workflows.prepare.data_analysis import DataTools
from athena.workflows.prepare.evaluator_factory import EvaluatorFactory
from athena.workflows.search.code_agent import (
    CodeExecutionError,
    CodegenResult,
)


def frozen_eval_spec() -> EvalSpec:
    return EvalSpec(
        primary=MetricDef(
            name="f1_macro",
            direction="maximize",
            description="Macro-averaged F1",
        )
    )


def test_evaluator_factory_classification():
    task = TaskMetaData(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )
    profile = DataProfile(row_count=1000, col_count=10, task_type_hint="classification")
    spec = EvaluatorFactory.build(task, profile)
    assert spec.primary.name == "f1_macro"
    assert spec.primary.direction == "maximize"


def test_data_tools_sample():
    import tempfile, os
    import pandas as pd

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test.csv")
        pd.DataFrame({"a": range(100), "b": range(100)}).to_csv(path, index=False)
        tools = DataTools(path)
        ref = tools.sample(seed=42, n=10)
        sampled_path = ref.split("://", 1)[1]
        df = pd.read_csv(sampled_path)
        assert len(df) == 10
        assert list(df.columns) == ["a", "b"]


def test_data_profile_keeps_seven_persistent_fields() -> None:
    assert list(DataProfile.model_fields) == [
        "row_count",
        "col_count",
        "columns",
        "missing_rate",
        "task_type_hint",
        "target_col",
        "issue_summary",
    ]


def test_prepare_dataset_stores_raw_cleaned_and_disjoint_splits() -> None:
    raw = pd.DataFrame({"value": range(10), "label": [0, 1] * 5})
    cleaned = raw.assign(value=raw["value"] * 2)
    stored: dict[str, pd.DataFrame] = {}

    def put_frame(frame: pd.DataFrame) -> str:
        ref = f"artifact://frame-{len(stored)}"
        stored[ref] = frame
        return ref

    log = pipeline.prepare_dataset(
        raw,
        cleaned,
        seed=42,
        validation_ratio=0.2,
        test_ratio=0.2,
        put_frame=put_frame,
    )

    assert stored[log.raw_copy].equals(raw)
    assert stored[log.cleaned_data].equals(cleaned)
    assert stored[log.raw_copy] is not raw
    assert stored[log.cleaned_data] is not cleaned
    index_sets = [
        set(stored[log.splits[name]].index) for name in ("train", "validation", "test")
    ]
    assert not (
        index_sets[0] & index_sets[1]
        or index_sets[0] & index_sets[2]
        or index_sets[1] & index_sets[2]
    )
    assert set().union(*index_sets) == set(range(len(cleaned)))


def test_search_inputs_exclude_final_test_reference() -> None:
    log = ProcessingLog(
        raw_copy="artifact://raw",
        cleaned_data="artifact://cleaned",
        splits={
            "train": "artifact://train",
            "validation": "artifact://validation",
            "test": "artifact://final-test",
        },
    )

    assert pipeline.search_input_refs(log) == (
        "artifact://cleaned",
        "artifact://train",
        "artifact://validation",
    )


@pytest.mark.asyncio
async def test_data_pipeline_requests_the_fixed_analysis_seeds(tmp_path) -> None:
    tasks: list[str] = []

    class RecordingAgent:
        async def run(self, task: str) -> DataProfile:
            tasks.append(task)
            return DataProfile(row_count=12, col_count=2)

    profile = await pipeline.DataPipeline(RecordingAgent()).run(str(tmp_path / "data"))

    assert "17/42/97" in tasks[0]
    assert profile.row_count == 12


@pytest.mark.asyncio
async def test_data_pipeline_profiles_a_real_csv_artifact(tmp_path) -> None:
    workspace = tmp_path / "data"
    workspace.mkdir()
    pd.DataFrame({"value": [1, 2, None], "label": [0, 1, 1]}).to_csv(
        workspace / "cleaned.csv", index=False
    )

    class ArtifactAgent:
        async def run(self, task: str) -> None:
            return None

    profile = await pipeline.DataPipeline(ArtifactAgent()).run(str(workspace))

    assert profile.row_count == 3
    assert profile.col_count == 2
    assert profile.missing_rate > 0
    assert profile.columns[0].name == "value"


@pytest.mark.asyncio
async def test_data_pipeline_rejects_a_placeholder_result(tmp_path) -> None:
    class EmptyAgent:
        async def run(self, task: str) -> None:
            return None

    with pytest.raises(pipeline.PipelineError, match="without a DataProfile"):
        await pipeline.DataPipeline(EmptyAgent()).run(str(tmp_path / "data"))


@pytest.mark.asyncio
async def test_baseline_is_created_once_after_prepare(tmp_path) -> None:
    class Workspace:
        def __init__(self) -> None:
            self.calls = 0

        async def create(self, base_commit: str, branch: str) -> GitWorkBranch:
            self.calls += 1
            return GitWorkBranch(
                path=str(tmp_path), branch=branch, base_commit=base_commit
            )

    workspace = Workspace()
    tree = ResearchTree()

    class RecordingCodeAgent:
        def __init__(self) -> None:
            self.calls = 0
            self.result: CodegenResult | None = None

        async def execute(
            self,
            experiment_id,
            hypothesis,
            plan,
            parent_commit,
            eval_spec,
            worktree,
        ) -> CodegenResult:
            self.calls += 1
            assert tree.get_experiment(experiment_id).status is ExperimentStatus.RUNNING
            assert plan.kind == "baseline"
            self.result = CodegenResult(
                experiment_id=experiment_id,
                commit=parent_commit,
                diff=f"artifact://diffs/{experiment_id}",
                eval=EvalResult(
                    experiment_id=experiment_id,
                    primary=0.71,
                    per_sample=f"artifact://samples/{experiment_id}",
                ),
                logs=f"artifact://logs/{experiment_id}",
            )
            return self.result

    code_agent = RecordingCodeAgent()
    profile = DataProfile(row_count=10, col_count=2)
    log = ProcessingLog(
        raw_copy="artifact://raw",
        cleaned_data="artifact://cleaned",
        splits={
            "train": "artifact://train",
            "validation": "artifact://validation",
            "test": "artifact://test",
        },
    )

    first_id = await create_baseline(
        workspace,
        "a" * 40,
        tree,
        data_profile=profile,
        processing_log=log,
        eval_spec=frozen_eval_spec(),
        code_agent=code_agent,
    )
    second_id = await create_baseline(
        workspace,
        "a" * 40,
        tree,
        data_profile=profile,
        processing_log=log,
        eval_spec=frozen_eval_spec(),
        code_agent=code_agent,
    )

    assert first_id == second_id
    assert workspace.calls == 1
    assert code_agent.calls == 1
    assert tree.root_experiment_ids() == [first_id]
    baseline = tree.get_experiment(first_id)
    assert baseline.status is ExperimentStatus.SUCCEEDED
    assert baseline.eval == code_agent.result.eval
    assert baseline.artifacts == {
        "diff": code_agent.result.diff,
        "logs": code_agent.result.logs,
    }
    assert tree.best_experiment_id() == first_id
    hypothesis = tree.get_hypothesis(baseline.hypothesis_id)
    assert hypothesis.status == "SUPPORTED"
    assert hypothesis.evidence_refs == [
        "artifact://cleaned",
        "artifact://train",
    ]


@pytest.mark.asyncio
async def test_baseline_accepts_a_structured_agent_draft(tmp_path) -> None:
    class Workspace:
        async def create(self, base_commit: str, branch: str) -> GitWorkBranch:
            return GitWorkBranch(
                path=str(tmp_path), branch=branch, base_commit=base_commit
            )

    class Agent:
        async def run(self, prompt: str, *, output_type):
            assert output_type is BaselineDraft
            return BaselineDraft(
                statement="A linear model is the reproducible baseline",
                intervention="Train logistic regression on the cleaned train split",
                expected_effect="Establish the reference macro F1",
                change="Add a logistic-regression training entrypoint",
                budget={"max_trials": 1},
                acceptance_rule="The frozen evaluator completes",
                rubrics=["No final-test access"],
            )

    class CodeAgent:
        async def execute(
            self,
            experiment_id,
            hypothesis,
            plan,
            parent_commit,
            eval_spec,
            worktree,
        ) -> CodegenResult:
            return CodegenResult(
                experiment_id=experiment_id,
                commit=parent_commit,
                diff=f"artifact://diffs/{experiment_id}",
                eval=EvalResult(
                    experiment_id=experiment_id,
                    primary=0.72,
                    per_sample=f"artifact://samples/{experiment_id}",
                ),
                logs=f"artifact://logs/{experiment_id}",
            )

    log = ProcessingLog(
        raw_copy="artifact://raw",
        cleaned_data="artifact://cleaned",
        splits={
            "train": "artifact://train",
            "validation": "artifact://validation",
            "test": "artifact://test",
        },
    )
    tree = ResearchTree()
    experiment_id = await create_baseline(
        Workspace(),
        "b" * 40,
        tree,
        data_profile=DataProfile(row_count=10, col_count=2),
        processing_log=log,
        eval_spec=frozen_eval_spec(),
        code_agent=CodeAgent(),
        agent=Agent(),
    )

    experiment = tree.get_experiment(experiment_id)
    hypothesis = tree.get_hypothesis(experiment.hypothesis_id)
    assert hypothesis.statement.startswith("A linear model")
    assert experiment.plan.rubrics == ["No final-test access"]


@pytest.mark.asyncio
async def test_failed_baseline_records_error_and_blocks_retry(tmp_path) -> None:
    class Workspace:
        async def create(self, base_commit: str, branch: str) -> GitWorkBranch:
            return GitWorkBranch(
                path=str(tmp_path), branch=branch, base_commit=base_commit
            )

    class FailingCodeAgent:
        async def execute(self, *args, **kwargs) -> CodegenResult:
            raise CodeExecutionError("training failed", logs="artifact://logs/baseline")

    tree = ResearchTree()
    log = ProcessingLog(
        raw_copy="artifact://raw",
        cleaned_data="artifact://cleaned",
        splits={
            "train": "artifact://train",
            "validation": "artifact://validation",
            "test": "artifact://test",
        },
    )

    with pytest.raises(CodeExecutionError, match="training failed"):
        await create_baseline(
            Workspace(),
            "c" * 40,
            tree,
            data_profile=DataProfile(row_count=10, col_count=2),
            processing_log=log,
            eval_spec=frozen_eval_spec(),
            code_agent=FailingCodeAgent(),
        )

    [baseline_id] = tree.root_experiment_ids()
    baseline = tree.get_experiment(baseline_id)
    assert baseline.status is ExperimentStatus.FAILED
    assert baseline.error == "training failed"
    assert baseline.artifacts["logs"] == "artifact://logs/baseline"
    assert tree.best_experiment_id() is None

    with pytest.raises(RuntimeError, match="failed baseline"):
        await create_baseline(
            Workspace(),
            "c" * 40,
            tree,
            data_profile=DataProfile(row_count=10, col_count=2),
            processing_log=log,
            eval_spec=frozen_eval_spec(),
            code_agent=FailingCodeAgent(),
        )
