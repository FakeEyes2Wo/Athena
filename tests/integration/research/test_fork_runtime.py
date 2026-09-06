"""分叉出来的实验臂，起 runtime 之后是不是真的继承了同一个 evaluator 与基线。

单元测试验的是 ``fork_project`` 写出来的文件对不对；这一条验的是**宿主读不读得到**。
两者之间正是这条链路反复出事的地方——``corpus_ref`` 曾经因为写在一个库、读在另一个库而
长期取不到，而症状只是"Ideator 一个算子都没有"。

A/B 的全部前提就是这三样一致：同一个冻结 evaluator、同一个基线 commit、同一份 artifact。
它们不一致时不会报错，只会让两臂的分数悄悄不可比。
"""

import json
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.research.fork import fork_project
from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.state import ResearchState

EVALUATOR_REF = "sha256:" + "a" * 64


async def _prepared_project(root: Path) -> str:
    """造一个"PREPARE 已完成"的项目：真 git 仓库、真 artifact、真 research_tree。"""
    athena = root / ".athena"
    athena.mkdir(parents=True)
    store = LocalArtifactStore(athena / "artifacts")
    evaluator_ref = await store.put_text(json.dumps({"eval_script": "evaluate.py"}))
    workspace = LocalGitWorkspace(
        athena / "repo", athena / "workspaces", store.put_bytes
    )
    await workspace.init(initial_file="solution.py", initial_content="print(1)\n")

    tree = {
        "version": 2,
        "sota_id": "exp_baseline",
        "hypotheses": {
            "hyp_baseline": {
                "id": "hyp_baseline",
                "statement": "baseline",
                "intervention": "train the baseline",
                "expected_effect": "establish a reference score",
                "status": "SUPPORTED",
                "order": 1,
            }
        },
        "experiments": {
            "exp_baseline": {
                "id": "exp_baseline",
                "kind": "baseline",
                "hypothesis_id": "hyp_baseline",
                "commit": "0" * 40,
                "plan": {
                    "kind": "baseline",
                    "change": "train the baseline",
                    "rubrics": [],
                    "run_config_ref": evaluator_ref,
                    "budget": {},
                    "acceptance_rule": "primary metric improves",
                },
                "gitwork": {
                    "path": str(athena / "repo"),
                    "branch": "main",
                    "base_commit": "0" * 40,
                },
                "status": "SUCCEEDED",
                "eval": {
                    "experiment_id": "exp_baseline",
                    "primary": 0.8823,
                    "secondary": {"roc_auc": 0.8823},
                    "per_sample": "sha256:" + "c" * 64,
                },
            }
        },
    }
    (athena / "research_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    ResearchState(
        status="WAITING",
        phase="SEARCH",
        search_limit=6,
        concurrency=1,
        corpus_ref="sha256:" + "b" * 64,
        corpus_ideated_ref="sha256:" + "b" * 64,
    ).save(athena / "state.json")
    return evaluator_ref


@pytest.mark.asyncio
async def test_both_arms_inherit_one_evaluator_and_start_at_search(
    tmp_path: Path,
) -> None:
    """A/B 的前提：两臂共用 evaluator 与基线，且各自没有语料。"""
    source = tmp_path / "prepared"
    evaluator_ref = await _prepared_project(source)

    arms = []
    for name in ("arm-off", "arm-on"):
        target = tmp_path / name
        result = fork_project(source, target)
        assert result.evaluator_ref == evaluator_ref
        runtime = ResearchRuntime(project_root=target)
        try:
            arms.append(
                {
                    "evaluator": runtime.supervisor.evaluator_ref,
                    "phase": runtime.state.phase,
                    "corpus": runtime.state.corpus_ref,
                    "sota": runtime.tree.best_experiment_id(),
                }
            )
        finally:
            await runtime.aclose()

    assert arms[0] == arms[1], "两臂的起点必须逐字段相同，否则分数不可比"
    assert arms[0]["evaluator"] == evaluator_ref
    assert arms[0]["phase"] == "SEARCH"
    assert arms[0]["corpus"] is None
    assert arms[0]["sota"] == "exp_baseline"


@pytest.mark.asyncio
async def test_the_forked_arm_can_resolve_the_shared_artifacts(tmp_path: Path) -> None:
    """``corpus_ref`` 曾经因为写在一个库、读在另一个库而长期取不到，症状只是"没有算子"。"""
    source = tmp_path / "prepared"
    evaluator_ref = await _prepared_project(source)
    target = tmp_path / "arm"

    fork_project(source, target)

    store = LocalArtifactStore(target / ".athena" / "artifacts")
    assert (
        json.loads(await store.get_text(evaluator_ref))["eval_script"] == "evaluate.py"
    )


@pytest.mark.asyncio
async def test_the_baseline_git_history_comes_along(tmp_path: Path) -> None:
    """没有基线 commit，SEARCH 的候选无处可分叉。"""
    source = tmp_path / "prepared"
    await _prepared_project(source)
    target = tmp_path / "arm"

    fork_project(source, target)

    assert (target / ".athena" / "repo" / "solution.py").is_file()
