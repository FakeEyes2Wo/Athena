"""端到端试运行：PREPARE → SEARCH，使用合成数据和真实 Git 工作区。

用法：uv run python examples/trial_run.py
"""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
from pydantic_ai import Agent as PydanticAgent

# 抑制已知的 MVP 警告
warnings.filterwarnings("ignore", message="实验结果无法转换为数字")

# 将项目根目录加入路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from athena.core.research_models import ExperimentPlan
from athena.core.research_tree import ResearchTree
from athena.evaluation import Comparator
from athena.evaluation.types import ComparisonVerdict, EvalResult
from athena.experiment.ranking import HypothesisRanker, ProximityGraph
from athena.git_workspace import LocalGitWorkspace
from athena.research.budget import BudgetSnapshot
from athena.research.models import MetricSpec, TaskMetaData
from athena.workflows.prepare.evaluator_factory import EvaluatorFactory
from athena.data.types import ColumnSummary, DataProfile, ProcessingLog
from athena.workflows.prepare.data_analysis import DataTools
from athena.workflows.prepare.baseline import create_baseline
from athena.workflows.search.idea_generation import generate_hypotheses, PaperSearch
from athena.workflows.search.code_agent import CodeAgent
from athena.experiment.supervisor import Supervisor
from athena.ideator import Ideator
from athena.storage.artifact_store import LocalArtifactStore


async def main():
    print("=" * 60)
    print("Athena AI4ML — Trial Run")
    print("=" * 60)

    # 设置：数据和 git 仓库的临时目录
    data_dir = Path(tempfile.mkdtemp(prefix="athena_trial_data_"))
    git_dir = Path(tempfile.mkdtemp(prefix="athena_trial_git_"))
    worktree_dir = Path(tempfile.mkdtemp(prefix="athena_trial_wt_"))
    print(f"\nData dir:     {data_dir}")
    print(f"Git repo:     {git_dir}")
    print(f"Worktree dir: {worktree_dir}")

    # 第 1 步：创建合成数据集
    print("\n" + "=" * 60)
    print("STEP 1: Create Synthetic Dataset")
    print("=" * 60)

    np.random.seed(42)

    n = 500
    X = np.random.randn(n, 8)
    # 使 target 依赖于前 3 个特征 + 噪声
    logits = 0.5 * X[:, 0] + 0.3 * X[:, 1] - 0.4 * X[:, 2] + 0.1 * np.random.randn(n)
    y = (1 / (1 + np.exp(-logits)) > 0.5).astype(int)

    cols = [f"feature_{i}" for i in range(8)]
    df = pd.DataFrame(X, columns=cols)
    df["target"] = y
    data_path = data_dir / "trial_data.csv"
    df.to_csv(data_path, index=False)
    print(f"Dataset: {len(df)} rows × {len(df.columns)} cols")
    print(f"Target distribution: {dict(df['target'].value_counts().to_dict())}")
    print(f"Saved to: {data_path}")

    # 第 2 步：定义任务
    print("\n" + "=" * 60)
    print("STEP 2: Define Task")
    print("=" * 60)

    task = TaskMetaData(
        task_type="classification",
        data_type="tabular",
        target_vars=["target"],
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )
    print(f"Task: {task.task_type} / {task.data_type}")
    print(f"Target: {task.target_vars}")
    print(
        f"Primary metric: {task.primary_metric.name} ({task.primary_metric.direction})"
    )

    # 第 3 步：数据分析
    print("\n" + "=" * 60)
    print("STEP 3: Data Analysis")
    print("=" * 60)

    tools = DataTools(str(data_path.resolve()))
    profile = DataProfile(
        row_count=len(df),
        col_count=len(df.columns),
        columns=[
            ColumnSummary(
                name=c,
                dtype=str(df[c].dtype),
                missing_rate=float(df[c].isnull().mean()),
                n_unique=(int(df[c].nunique()) if df[c].dtype == "object" else None),
                sample_values=[str(v) for v in df[c].head(5).tolist()],
            )
            for c in df.columns
        ],
        missing_rate=float(df.isnull().mean().mean()),
        task_type_hint="classification",
        target_col="target",
        issue_summary="No missing values, balanced classes. Synthetic dataset — ready for modeling.",
    )
    print(f"Rows: {profile.row_count}, Cols: {profile.col_count}")
    print(f"Missing rate: {profile.missing_rate:.4f}")
    print(f"Task hint: {profile.task_type_hint}")

    # 测试 DataTools
    sample_ref = tools.sample(seed=42, n=100)
    desc = tools.describe(sample_ref)
    print(f"Sample created: {sample_ref}")
    print(f"Describe output length: {len(desc)} chars")

    # 第 4 步：构建评估规格
    print("\n" + "=" * 60)
    print("STEP 4: Build Evaluation Spec")
    print("=" * 60)

    spec = EvaluatorFactory.build(task, profile)
    print(f"Primary:   {spec.primary.name} ({spec.primary.direction})")
    print(f"Secondary: {[m.name for m in spec.secondary]}")
    print(f"Split seed: {spec.split_seed}, Test ratio: {spec.test_ratio}")

    # 第 5 步：初始化 Git 工作区
    print("\n" + "=" * 60)
    print("STEP 5: Initialize Git Workspace")
    print("=" * 60)

    async def fake_diff_writer(diff_bytes: bytes) -> str:
        return f"artifact-{hashlib.sha256(diff_bytes).hexdigest()[:16]}"

    workspace = LocalGitWorkspace(git_dir, worktree_dir, fake_diff_writer)
    base_commit = await workspace.init()
    print(f"Git repo initialized: {git_dir}")
    print(f"Base commit: {base_commit[:12]}...")

    # 第 6 步：创建基线
    print("\n" + "=" * 60)
    print("STEP 6: Create Baseline")
    print("=" * 60)

    tree = ResearchTree()
    data_ref = f"artifact://{data_path.resolve()}"
    processing_log = ProcessingLog(
        raw_copy=data_ref,
        cleaned_data=data_ref,
        splits={
            "train": f"{data_ref}#train",
            "validation": f"{data_ref}#validation",
            "test": f"{data_ref}#test",
        },
    )
    code_agent = CodeAgent(backend="auto")
    baseline_id = await create_baseline(
        workspace,
        base_commit,
        tree,
        data_profile=profile,
        processing_log=processing_log,
        eval_spec=spec,
        code_agent=code_agent,
    )
    baseline = tree.get_experiment(baseline_id)
    baseline_hypothesis = tree.get_hypothesis(baseline.hypothesis_id)
    print(f"Baseline node: {baseline_hypothesis.statement}")
    print(f"Branch: {baseline.gitwork.branch}")
    print(f"Worktree path: {baseline.gitwork.path}")

    # 第 7 步：生成假设
    print("\n" + "=" * 60)
    print("STEP 7: Generate Hypotheses")
    print("=" * 60)

    search = PaperSearch()
    papers = await search.search("classification tabular machine learning", n=3)
    print(f"Papers found: {len(papers)} (MVP returns empty)")

    model = os.environ.get("ATHENA_IDEATOR_MODEL")
    if not model:
        raise RuntimeError("ATHENA_IDEATOR_MODEL is required for idea generation")
    ideator = Ideator(
        agent_factory=lambda _role, _index: PydanticAgent(model),
        artifacts=LocalArtifactStore(Path(".athena/artifacts")),
    )
    hypotheses = await generate_hypotheses(profile, papers, [], tree, ideator=ideator)
    print(f"Hypotheses generated: {len(hypotheses)}")
    for h in hypotheses:
        print(f"  [{(h.id or '?')[:12]}] {h.statement}")
        print(f"       Intervention: {h.intervention}")
        print(f"       Status: {h.status}")

    pending = tree.pending_hypotheses()
    print(f"Pending in tree: {len(pending)}")

    # 第 8 步：运行搜索循环（单次迭代）
    print("\n" + "=" * 60)
    print("STEP 8: Run Search Loop (1 iteration)")
    print("=" * 60)

    budget = BudgetSnapshot(remaining=3, max_no_improve=2)
    ranker = HypothesisRanker()
    proximity = ProximityGraph()
    supervisor = Supervisor()
    comparator = Comparator()

    # 手动单次迭代（避免完整循环需要 ArtifactStore）
    print("\n--- Iteration 1 ---")

    # Select
    if pending:
        selected_id = ranker.select(pending, proximity)
        h = next(hh for hh in hypotheses if hh.id == selected_id)
        print(f"Selected: [{(h.id or '?')[:12]}] {h.statement}")

        # 执行
        best_id = tree.best_experiment_id()
        best = tree.get_experiment(best_id) if best_id else None
        parent_commit = best.commit if best else base_commit
        branch_name = f"exp/{(h.id or '?')[:12]}"
        wt = await workspace.create(parent_commit, branch_name)
        print(f"Worktree created: {wt.path} ({wt.branch})")

        experiment_id = f"exp_{uuid4().hex[:8]}"
        plan = ExperimentPlan(
            kind="search",
            change=f"Apply the intervention '{h.intervention}'.",
            run_config_ref=f"artifact://runs/{experiment_id}/config",
            budget={"runs": 1},
            acceptance_rule="Improved primary metric over baseline.",
        )
        result = await code_agent.execute(
            experiment_id, h, plan, parent_commit, spec, wt
        )
        print(f"Experiment: {result.experiment_id}")
        print(f"Primary metric: {result.eval.primary:.4f}")
        print(f"Wall time: {result.wall_time_s:.2f}s")
        print(f"Logs ref: {result.logs}")

        # 直接读取评估结果文件
        eval_json = Path(wt.path) / "eval_result.json"
        if eval_json.exists():
            with open(eval_json) as f:
                eval_data = json.load(f)
            print(f"Eval JSON: {eval_data}")

        # 清理工作树（强制 — 试运行中预期有未提交的变更）
        await workspace.remove(wt, delete_branch=True, force=True)
        print("Worktree cleaned up")

        # 更新排序器
        ranker.update([(result.experiment_id, "baseline_root", True)])
        proximity.add(h)

        # 监督者检查
        if best and best.eval is not None:
            base_eval = EvalResult(
                experiment_id=best_id or "baseline",
                primary=best.eval.primary,
                per_sample="artifact://samples/baseline",
            )
            verdict = comparator.compare(base_eval, result.eval)
        else:
            verdict = ComparisonVerdict(winner="candidate", p_value=0.0)

        decision = supervisor.decide(verdict, budget)
        budget.consume(improved=(verdict.winner == "candidate"))
        print(f"Verdict: {verdict.winner} (p={verdict.p_value:.4f})")
        print(f"Decision: {decision.action} — {decision.reason}")
        print(
            f"Budget: {budget.remaining} remaining, streak={budget.no_improve_streak}"
        )

    # 第 9 步：总结
    print("\n" + "=" * 60)
    print("TRIAL RUN COMPLETE")
    print("=" * 60)
    print(f"Tree experiments: {len(tree.to_dict()['experiments'])}")
    print(f"Hypotheses: {len(tree.to_dict()['hypotheses'])}")
    print(f"Budget consumed: {3 - budget.remaining}/{3}")
    print(f"\nTemp dirs (not cleaned up for inspection):")
    print(f"  Data:  {data_dir}")
    print(f"  Git:   {git_dir}")
    print(f"  WT:    {worktree_dir}")
    print("\nAll pipeline stages executed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
