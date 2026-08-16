"""从一个已经 PREPARE 完的项目分叉出若干条实验臂。

这是"文献接地到底有没有用"这个问题唯一能被回答的形态。上一次 A/B 答不了它，原因不在
执行而在设计：**两臂各自跑了自己的 PREPARE**，于是各自写出一个 evaluator、各自训出一个
基线。同一任务、同一数据、同一留出集，两个基线的强弱就能差 0.03 以上，而 SEARCH 的改进
也就 +0.05 量级——基线方差与待测效应同量级，N=1 的两臂什么也测不出来，不管结果是正是负。

分叉把这三样固定下来：

- **同一个冻结 evaluator**（``research_tree.json`` 里 baseline 实验的 ``run_config_ref``）
- **同一个基线 commit**（``.athena/repo`` 的 git 历史）
- **同一份 artifact 存储**（预测、报告、EDA 全部按内容寻址，复制即等同）

只让 ideation 不同。两臂的分数因此第一次真正可比。

## 为什么是复制而不是共享

共享一份 ``.athena`` 会让两臂互相写坏对方的 ``state.json`` 与 git 工作区——Supervisor 是
单写者，前提是它独占那份状态。内容寻址让复制的代价只在 artifact 上，而那部分本来就是
去重的。
"""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

FORKED_DIRECTORIES = ("artifacts", "repo")
"""分叉要整份带走的目录。

``artifacts`` 是内容寻址的，带走它两臂才能解析同一批引用（冻结 evaluator 的 bundle、
基线的预测与报告都在里面）。``repo`` 是基线 commit 所在的 git 历史——没有它，SEARCH 的
候选无处可分叉。

``workspaces`` / ``runs`` / ``logs`` 刻意不带：那是上一次运行的临时产物，复制过来只会
让新臂从一个半旧的工作区起步。
"""

RESET_STATE_FIELDS = {
    "status": "RUNNING",
    "phase": "SEARCH",
    "plans": {},
    "validation": None,
    "corpus_ref": None,
    "corpus_ideated_ref": None,
}
"""分叉后要重置的状态字段。

``phase`` 直接落到 SEARCH：PREPARE 的产物已经带过来了，重跑一遍就等于把要固定的那两样
又变成变量。``plans`` 与 ``validation`` 属于上一次运行的进度，留着会让新臂以为自己已经
跑过几轮。

``corpus_ref`` / ``corpus_ideated_ref`` 必须清空——它们正是要被比较的那个变量。不清的话
关调研那一臂会继承开调研那一臂的语料，A/B 直接失去意义。
"""


class ForkError(RuntimeError):
    """源项目不具备分叉条件，或目标已存在。"""


@dataclass(frozen=True, slots=True)
class ForkResult:
    """一次分叉的结果，供 CLI 打印与用例断言。"""

    source: Path
    target: Path
    evaluator_ref: str
    baseline_experiment_id: str
    copied: tuple[str, ...]


def _baseline(tree: dict) -> dict:
    """取出基线实验；没有它就没有可共享的评估与起点。"""
    experiments = tree.get("experiments")
    items = experiments.values() if isinstance(experiments, dict) else experiments or []
    for experiment in items:
        if isinstance(experiment, dict) and experiment.get("kind") == "baseline":
            return experiment
    raise ForkError(
        "source project has no baseline experiment; run PREPARE there first — "
        "forking exists precisely to avoid re-running it per arm"
    )


def _evaluator_ref(experiment: dict) -> str:
    plan = experiment.get("plan")
    ref = plan.get("run_config_ref") if isinstance(plan, dict) else None
    if not isinstance(ref, str) or not ref:
        raise ForkError(
            "baseline experiment carries no frozen evaluator reference; the source "
            "project's PREPARE did not complete"
        )
    return ref


def fork_project(source: str | Path, target: str | Path) -> ForkResult:
    """把 ``source`` 的 PREPARE 产物复制成一条新的实验臂。

    幂等性刻意不提供：目标已存在时报错而不是覆盖。一条臂跑到一半被另一条覆盖掉，表现
    出来是"分数怎么变了"，而不是一条错误信息。
    """
    source_root = Path(source).resolve()
    target_root = Path(target).resolve()
    source_athena = source_root / ".athena"
    target_athena = target_root / ".athena"
    if not source_athena.is_dir():
        raise ForkError(f"{source_athena} does not exist; nothing to fork")
    if target_athena.exists():
        raise ForkError(
            f"{target_athena} already exists; pick a fresh directory so a half-run "
            "arm cannot be silently overwritten"
        )
    tree_path = source_athena / "research_tree.json"
    state_path = source_athena / "state.json"
    for required in (tree_path, state_path):
        if not required.is_file():
            raise ForkError(f"{required} is missing; the source project is incomplete")

    tree = json.loads(tree_path.read_text(encoding="utf-8"))
    baseline = _baseline(tree)
    evaluator_ref = _evaluator_ref(baseline)

    target_athena.mkdir(parents=True)
    copied: list[str] = []
    for name in FORKED_DIRECTORIES:
        origin = source_athena / name
        if origin.is_dir():
            shutil.copytree(origin, target_athena / name)
            copied.append(name)
    shutil.copy2(tree_path, target_athena / "research_tree.json")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(RESET_STATE_FIELDS)
    (target_athena / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return ForkResult(
        source=source_root,
        target=target_root,
        evaluator_ref=evaluator_ref,
        baseline_experiment_id=str(baseline.get("id", "")),
        copied=tuple(copied),
    )
