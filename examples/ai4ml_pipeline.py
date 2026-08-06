"""最简端到端 AI4ML 流水线示例。

演示：任务定义 -> 数据分析 -> 评估规格 -> 假设生成 -> 预算设置 -> 监督者决策。

用法：uv run python examples/ai4ml_pipeline.py
"""

import asyncio
import os
from pathlib import Path

from pydantic_ai import Agent as PydanticAgent

from athena.core.research_tree import ResearchTree
from athena.evaluation.types import ComparisonVerdict
from athena.experiment.supervisor import Supervisor
from athena.ideator import Ideator
from athena.research.budget import BudgetSnapshot
from athena.research.models import MetricSpec, TaskMetaData
from athena.storage.artifact_store import LocalArtifactStore
from athena.workflows.prepare.evaluator_factory import EvaluatorFactory
from athena.data.types import DataProfile
from athena.workflows.search.idea_generation import generate_hypotheses


async def main() -> None:
    # 1. 定义任务
    print("=" * 60)
    print("STEP 1: Define Task")
    print("=" * 60)
    task = TaskMetaData(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )
    print(f"  Task type          : {task.task_type}")
    print(f"  Data type          : {task.data_type}")
    print(f"  Primary metric     : {task.primary_metric.name}")
    print(f"  Metric direction   : {task.primary_metric.direction}")

    # 2. 分析数据
    print("\n" + "=" * 60)
    print("STEP 2: Analyze Data")
    print("=" * 60)
    profile = DataProfile(
        row_count=1000,
        col_count=20,
        task_type_hint="classification",
        missing_rate=0.03,
        target_col="target",
    )
    print(f"  Rows               : {profile.row_count}")
    print(f"  Columns            : {profile.col_count}")
    print(f"  Task hint          : {profile.task_type_hint}")
    print(f"  Missing rate       : {profile.missing_rate}")

    # 3. 构建评估规格
    print("\n" + "=" * 60)
    print("STEP 3: Build Evaluation Spec")
    print("=" * 60)
    spec = EvaluatorFactory.build(task, profile)
    print(f"  Primary metric     : {spec.primary.name}")
    print(f"  Direction          : {spec.primary.direction}")
    print(f"  Secondary metrics  : {[m.name for m in spec.secondary]}")
    print(f"  Split seed         : {spec.split_seed}")
    print(f"  Test ratio         : {spec.test_ratio}")

    # 4. 初始化研究树并生成假设
    print("\n" + "=" * 60)
    print("STEP 4: Generate Hypotheses")
    print("=" * 60)
    tree = ResearchTree()
    model = os.environ.get("ATHENA_IDEATOR_MODEL")
    if not model:
        raise RuntimeError("ATHENA_IDEATOR_MODEL is required for idea generation")
    ideator = Ideator(
        agent_factory=lambda _role, _index: PydanticAgent(model),
        artifacts=LocalArtifactStore(Path(".athena/artifacts")),
    )
    hypotheses = await generate_hypotheses(profile, [], [], tree, ideator=ideator)
    print(f"  Generated {len(hypotheses)} hypotheses:")
    for h in hypotheses:
        print(f"    [{h.id}] {h.statement}")
        print(f"      Intervention : {h.intervention}")
        print(f"      Effect      : {h.expected_effect}")

    # 检查树状态
    pending = tree.pending_hypotheses()
    print(f"\n  Pending hypotheses in tree: {len(pending)}")

    # 5. 设置预算和监督者
    print("\n" + "=" * 60)
    print("STEP 5: Budget and Supervisor")
    print("=" * 60)
    budget = BudgetSnapshot(remaining=10, max_no_improve=3)
    supervisor = Supervisor()
    print(f"  Remaining experiments   : {budget.remaining}")
    print(f"  Max no-improve streak   : {budget.max_no_improve}")

    # 演示监督者决策
    scenarios = [
        ("Candidate wins", ComparisonVerdict(winner="candidate", p_value=0.01)),
        ("No improvement", ComparisonVerdict(winner="baseline", p_value=0.5)),
        ("Tie", ComparisonVerdict(winner="tie", p_value=0.9)),
    ]
    print("\n  Supervisor decisions:")
    for label, verdict in scenarios:
        d = supervisor.decide(verdict, budget)
        print(f"    {label:20s} -> {d.action:6s}  ({d.reason})")

    # 6. 总结
    print("\n" + "=" * 60)
    print("PIPELINE SETUP COMPLETE")
    print("=" * 60)
    print(f"  Task defined        : {task.task_type}/{task.data_type}")
    print(f"  Evaluation spec     : {spec.primary.name}")
    print(f"  Hypotheses ready    : {len(hypotheses)}")
    print(f"  Budget allocated    : {budget.remaining} experiments")
    print()
    print("Ready to enter SEARCH loop.")


if __name__ == "__main__":
    asyncio.run(main())
