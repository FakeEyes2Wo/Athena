from athena.core.schemas import Hypothesis, ExperimentPlan
from athena.core.research.research_tree import Experiment, ResearchTree
from athena.core.gitutils.workspace import GitWorkBranch, GitWorkspace


async def create_baseline(
    workspace: GitWorkspace,
    base_commit: str,
    research_tree: ResearchTree,
) -> Experiment:
    """Create a baseline experiment as the root of the ResearchTree."""
    from uuid import uuid4

    branch = f"baseline-{uuid4().hex[:8]}"
    wt = await workspace.create(base_commit, branch)
    h = Hypothesis(
        id=f"hyp_{uuid4().hex[:12]}",
        statement="Baseline model with default configuration",
        intervention="Train baseline model on raw data",
        expected_effect="Establish baseline metric",
        status="PROPOSED",
    )
    plan = ExperimentPlan(
        kind="baseline",
        change="Initial baseline training",
        run_config_ref=f"artifact://runs/baseline-{uuid4().hex[:8]}",
        budget={"epochs": 10},
        acceptance_rule="None — baseline by definition",
        rubrics=[],
    )
    exp = Experiment(
        commit=base_commit,
        hypothesis=h,
        plan=plan,
        metric_type="primary",
        result="N/A",  # filled after execution
        gitwork=wt,
    )
    research_tree.create_node(exp)  # root, no parent
    return exp
