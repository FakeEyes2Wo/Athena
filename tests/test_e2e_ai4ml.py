"""端到端冒烟测试：PREPARE -> SEARCH -> VALIDATE -> REPORT。"""

import pytest
import asyncio


@pytest.mark.asyncio
async def test_main_composition_runs_prepare_search_validate_report_offline(
    tmp_path,
) -> None:
    """The real composition closes the full loop with external agents replaced."""
    import json
    import shutil
    from pathlib import Path

    import pandas as pd

    from athena.code.backends.base import CodeBackend
    from athena.code.types import GenerationResult
    from athena.core.research_models import Hypothesis
    from athena.core.research_tree import ExperimentStatus
    from athena.ideator import DebateResult
    from athena.storage.artifact_store import LocalArtifactStore
    from athena.workflows.report import Reporter
    from src.main import RunConfig, build_application, drive_runtime

    MODEL_SOURCE = (
        "def predict(features, row_id):\n" "    return features[row_id].fillna(0) % 2\n"
    )
    RUN_SOURCE = """import json
from pathlib import Path

import pandas as pd
from model import predict

manifest = json.loads(Path(".athena/phase_manifest.json").read_text(encoding="utf-8"))
features = pd.read_csv(manifest["predict"])
row_id = manifest["row_id_column"]
frame = pd.DataFrame(
    {row_id: features[row_id], "prediction": predict(features, row_id)}
)
frame.to_csv("predictions.csv", index=False)
"""

    data_path = tmp_path / "dataset.csv"
    pd.DataFrame(
        {
            "feature": list(range(30)),
            "label": [index % 2 for index in range(30)],
        }
    ).to_csv(data_path, index=False)

    class OfflineIdeator:
        async def generate(self, data_profile, papers, models, tree):
            parent_id = tree.best_experiment_id()
            assert parent_id is not None
            return DebateResult(
                hypotheses=[
                    Hypothesis(
                        id=f"hyp-offline-search-{index}",
                        parent_id=parent_id,
                        statement=f"Reuse deterministic target relation {index}",
                        intervention=f"Fit deterministic validation predictor {index}",
                        expected_effect="Preserve the frozen primary metric",
                        sources=["offline-fixture"],
                        evidence_refs=["artifact://offline/evidence"],
                    )
                    for index in range(3)
                ],
                transcript=[{"stage": "judge", "agent": "offline"}],
                failures=[],
                artifact_ref="artifact://offline/debate",
            )

    class OfflineBackend(CodeBackend):
        """Trusted-contract backend: reads the phase manifest and emits row-ID
        keyed predictions (never labels), plus one auxiliary model.py."""

        def __init__(self) -> None:
            self.seen: list[tuple[str, str]] = []  # (plan kind, prompt)

        async def generate(self, prompt, target_dir, previous_outputs, history):
            context = json.loads(prompt.split("Frozen experiment context:\n", 1)[1])
            plan_kind = context["plan"]["kind"]
            experiment_id = context["experiment_id"]
            self.seen.append((plan_kind, prompt))
            root = Path(target_dir)
            manifest = json.loads(
                (root / ".athena" / "phase_manifest.json").read_text(encoding="utf-8")
            )
            assert manifest["predict"] == ".athena/inputs/predict.csv"
            (root / "model.py").write_text(
                f"# generated for {experiment_id}\n" + MODEL_SOURCE,
                encoding="utf-8",
            )
            (root / "run_experiment.py").write_text(RUN_SOURCE, encoding="utf-8")
            return GenerationResult(
                files_created=["model.py", "run_experiment.py"],
                files_modified=[],
            )

    backend = OfflineBackend()
    config = RunConfig(
        data=data_path,
        target="label",
        model="offline:test",
        backend="qoder",
        output_dir=tmp_path / "run",
        max_experiments=1,
        max_no_improve=1,
    )
    application = build_application(
        config,
        ideator=OfflineIdeator(),
        backends={"qoder": backend},
        reporter=Reporter(output_dir=config.output_dir / "reports"),
    )
    try:
        result = await drive_runtime(application.runtime, config)
    finally:
        await application.runtime.aclose()

    assert application.runtime.phase == "COMPLETED"
    assert Path(result["tree_path"]).is_file()
    report_path = Path(result["report_ref"].removeprefix("artifact://"))
    assert report_path.is_file()
    tree_payload = application.runtime.tree.to_dict()
    experiments = tree_payload["experiments"]
    successful = [
        experiment
        for experiment in experiments.values()
        if experiment["status"] == ExperimentStatus.SUCCEEDED.value
    ]
    assert len(successful) == 4
    for experiment in successful:
        assert len(experiment["commit"]) == 40
        assert set(experiment["artifacts"]) >= {"diff", "logs"}
        assert experiment["artifacts"]["diff"].startswith(("sha256:", "artifact://"))
        assert experiment["artifacts"]["logs"].startswith(("sha256:", "artifact://"))
        assert experiment["eval"]["per_sample"].startswith(("sha256:", "artifact://"))

    # final-test never invokes a backend and retains the exact SOTA commit.
    assert "final-test" not in {kind for kind, _ in backend.seen}
    sota_id = tree_payload["sota_id"]
    final_test = next(
        experiment
        for experiment in experiments.values()
        if experiment["plan"]["kind"] == "final-test"
    )
    assert final_test["commit"] == experiments[sota_id]["commit"]

    # SEARCH prompts must not carry final-test split paths.
    prepared = application.context.prepared
    test_features_path = str(prepared.test_inputs.features_path)
    search_prompts = [prompt for kind, prompt in backend.seen if kind == "search"]
    assert search_prompts
    assert all(test_features_path not in prompt for prompt in search_prompts)

    # Evidence is content-addressed and readable after worktree cleanup.
    store = LocalArtifactStore(config.output_dir.resolve() / "artifacts" / "objects")
    for experiment in experiments.values():
        worktree_path = Path(experiment["gitwork"]["path"])
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
    for experiment in successful:
        refs = [
            experiment["artifacts"].get("diff"),
            experiment["artifacts"].get("logs"),
            (experiment["eval"] or {}).get("per_sample"),
        ]
        sha_refs = [ref for ref in refs if ref and ref.startswith("sha256:")]
        assert sha_refs, "each successful experiment must retain durable evidence"
        for ref in sha_refs:
            await store.get_bytes(ref)


@pytest.mark.slow
def test_full_pipeline_smoke():
    """端到端冒烟测试，验证所有模块能正确连接。

    演练范围：TaskMetaData、DataProfile、EvaluatorFactory、ResearchTree、
    HypothesisRanker、Supervisor、SearchLoop、generate_hypotheses。
    """
    from athena.core.research_models import Hypothesis
    from athena.core.research_tree import ResearchTree
    from athena.evaluation.types import ComparisonVerdict
    from athena.experiment.supervisor import Supervisor
    from athena.ideator import DebateResult
    from athena.research.budget import BudgetSnapshot
    from athena.research.models import MetricSpec, TaskMetaData
    from athena.workflows.prepare.evaluator_factory import EvaluatorFactory
    from athena.data.types import DataProfile
    from athena.workflows.search.idea_generation import generate_hypotheses

    class RecordingIdeator:
        def __init__(self) -> None:
            self.calls = []

        async def generate(self, data_profile, papers, models, research_tree):
            self.calls.append((data_profile, papers, models, research_tree))
            return DebateResult(
                hypotheses=[
                    Hypothesis(
                        id=f"hyp-e2e-{index}",
                        parent_id=f"exp-parent-{index}",
                        statement=f"Offline hypothesis {index}",
                        intervention=f"Apply offline change {index}",
                        expected_effect="Improve the frozen primary metric",
                        sources=[f"paper-{index}"],
                        evidence_refs=[f"artifact://debate/evidence-{index}"],
                    )
                    for index in range(1, 4)
                ],
                transcript=[{"stage": "judge", "agent": "judge-0"}],
                failures=[],
                artifact_ref="artifact://debate/e2e-result",
            )

    task = TaskMetaData(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )
    profile = DataProfile(row_count=100, col_count=5, task_type_hint="classification")
    spec = EvaluatorFactory.build(task, profile)
    assert spec.primary.name == "f1_macro"
    assert spec.primary.direction == "maximize"

    tree = ResearchTree()
    budget = BudgetSnapshot(remaining=3, max_no_improve=2)

    ideator = RecordingIdeator()
    hypotheses = asyncio.run(
        generate_hypotheses(profile, [], [], tree, ideator=ideator)
    )
    assert ideator.calls == [(profile, [], [], tree)]
    assert len(hypotheses) == 3
    assert all(h.status == "PROPOSED" for h in hypotheses)
    assert [h.id for h in hypotheses] == [
        "hyp-e2e-1",
        "hyp-e2e-2",
        "hyp-e2e-3",
    ]
    assert [h.parent_id for h in hypotheses] == [
        "exp-parent-1",
        "exp-parent-2",
        "exp-parent-3",
    ]
    assert [h.evidence_refs for h in hypotheses] == [
        ["artifact://debate/evidence-1"],
        ["artifact://debate/evidence-2"],
        ["artifact://debate/evidence-3"],
    ]

    pending = tree.pending_hypotheses()
    assert pending == hypotheses
    assert all(h.status == "PROPOSED" for h in pending)

    supervisor = Supervisor()

    # 候选者胜出且预算有余 -> ACCEPT
    d1 = supervisor.decide(
        ComparisonVerdict(winner="candidate", p_value=0.01),
        BudgetSnapshot(remaining=5),
    )
    assert d1.action == "ACCEPT"

    # 基线胜出且接近连败上限 -> STOP
    d2 = supervisor.decide(
        ComparisonVerdict(winner="baseline", p_value=0.5),
        BudgetSnapshot(remaining=1, no_improve_streak=4, max_no_improve=5),
    )
    assert d2.action == "STOP"

    # 平局且无连败 -> REJECT
    d3 = supervisor.decide(
        ComparisonVerdict(winner="tie", p_value=0.9),
        BudgetSnapshot(remaining=5, max_no_improve=5),
    )
    assert d3.action == "REJECT"

    # 预算耗尽 -> 无论裁决结果均为 STOP
    d4 = supervisor.decide(
        ComparisonVerdict(winner="candidate", p_value=0.01),
        BudgetSnapshot(remaining=0, is_exhausted=True),
    )
    assert d4.action == "STOP"
