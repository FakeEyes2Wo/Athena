"""端到端冒烟测试：PREPARE -> SEARCH -> VALIDATE -> REPORT。"""

import pytest
import asyncio


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
