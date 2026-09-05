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

from athena.research.supervisor.state import _core_digest

FORKED_DIRECTORIES = ("artifacts", "repo")
"""分叉要整份带走的目录。

``artifacts`` 是内容寻址的，带走它两臂才能解析同一批引用（冻结 evaluator 的 bundle、
基线的预测与报告都在里面）。``repo`` 是基线 commit 所在的 git 历史——没有它，SEARCH 的
候选无处可分叉。

``runs`` / ``logs`` 刻意不带：那是上一次运行的临时产物，复制过来只会让新臂从一个半旧
的工作区起步。``workspaces`` 整体也不带，但其中的 **EDA 工作区是例外**，见
``_carry_eda_workspace``。
"""

CARRIED_RESUME_FIELDS = (
    "task_text",
    "data_contract",
    "evaluator_ref",
    "final_evaluator_ref",
    "kaggle_download",
)
"""PREPARE 的产物里存在 ``resume.json`` 而非 ``state.json`` 的那几项。

分叉曾经只复制 ``state.json``，而 ``ResearchState.save`` 把这些字段写进兄弟文件
``resume.json``。于是新臂开跑时它们全是 ``None``，两处静默出错：

- ``data_contract`` 为空 → SEARCH 候选拿不到数据契约（候选看不到任务原文，契约是
  它们唯一的来源），于是自己去数据目录里找划分，在被打分的那些行上训练。这正是
  ``data_contract_block`` 存在的理由，而分叉把它丢了。
- ``final_evaluator_ref`` 为空 → ``phase_runner`` 退回搜索 evaluator 并只记一条
  warning，新臂的 VALIDATE 于是拿 search 标签当留出集打分。

两者都只在分数上表现出来，而且是**更好看**的方向——A/B 里这一臂看起来赢了。

``task_research_*`` 刻意不带：与 ``corpus_ref`` 同类，属于要被比较的那个变量。
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


WORKTREE_LINK = ".git"
"""EDA 工作区里的 git worktree 链接文件。

它是一个指回**源项目** ``.athena/repo/worktrees/eda`` 的纯文本指针。复制过来必须删掉：
留着的话，新臂在 EDA 目录里跑的任何 git 命令都会作用到源项目的仓库上——两条臂于是共写
同一份历史，而症状不是报错，是分数莫名其妙地互相影响。EDA 目录在 SEARCH 期间只被当作
普通文件目录读写（``AgentTurnRunner._resolve_eda_dir`` 只校验它是不是本项目内的目录），
不需要它仍然是一个 worktree。
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


def _baseline(tree: dict) -> tuple[str, dict]:
    """取出基线实验及其 id；没有它就没有可共享的评估与起点。

    ``kind`` 在 ``plan`` 里，不在实验顶层——``Experiment`` 模型没有这个字段（见
    ``core.research_tree.Experiment``，它只有 ``parent_id``/``hypothesis_id``/``commit``/
    ``plan``/``gitwork``/``status``/``eval``/``verdict``/``artifacts``/``error``）。
    实验 id 同理：它是映射的键，不是记录里的字段。

    此前这里读的是顶层 ``kind`` 与顶层 ``id``，而单元测试的 fixture 恰好按那个形状造数据，
    于是 ``fork_project`` **在真项目上从未成功过一次**：它对着一个刚跑完 PREPARE 的项目
    抛 "source project has no baseline experiment"。2026-08-18 真机撞到。

    顶层 ``kind`` 仍然接受，只为兼容手写的旧树；判据以 ``plan.kind`` 为准。
    """
    experiments = tree.get("experiments")
    if not isinstance(experiments, dict):
        experiments = {}
    for experiment_id, experiment in experiments.items():
        if not isinstance(experiment, dict):
            continue
        plan = experiment.get("plan")
        kind = plan.get("kind") if isinstance(plan, dict) else None
        if kind == "baseline" or experiment.get("kind") == "baseline":
            return str(experiment_id), experiment
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


def _carry_eda_workspace(
    source_root: Path, target_root: Path, eda_dir: object
) -> str | None:
    """把 PREPARE 建好的 EDA 工作区一并带到新臂；带不了时返回 ``None``。

    这一份必须带。``state.eda_dir`` 是分叉保留的字段，而它指的目录在项目根下的
    ``workspaces/`` 里——那一层不在 ``FORKED_DIRECTORIES`` 中。不带的话新臂的
    ``state.eda_dir`` 指向一个不存在的目录，``AgentTurnRunner._resolve_eda_dir`` 抛
    "EDA workspace is stale"，**每个 Ideator lane 都失败**；而 ``run_ideator_turn``
    把 lane 异常吞成一条 error 输出，于是表现出来只是"这一臂一条假设都没有"。

    真机复现（2026-08-18）：分叉出的臂 ``workspaces/`` 整个不存在，``eda_dir`` 仍是
    ``workspaces/eda``。分叉存在的唯一目的就是跑 A/B，而它跑不出一条假设。

    路径按**源项目根**解析、按同一个相对位置写进目标：``eda_dir`` 存的是相对路径
    （见 ``phase_runner``），保持相对关系不变，目标项目的 state 才不用改写。
    """
    if not isinstance(eda_dir, str) or not eda_dir.strip():
        return None
    relative = Path(eda_dir)
    if relative.is_absolute():
        # 旧 state 遗留的绝对路径：它指向源项目，复制到新臂只会越界，交给
        # runtime 构造期的校验置空即可。
        return None
    origin = (source_root / relative).resolve()
    if not origin.is_dir() or not origin.is_relative_to(source_root):
        return None
    destination = target_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(origin, destination)
    (destination / WORKTREE_LINK).unlink(missing_ok=True)
    return relative.as_posix()


def _carry_resume_fields(
    source_athena: Path, target_athena: Path, target_core: dict
) -> None:
    """Copy the source's PREPARE resume fields under the target's own digest.

    ``resume.json`` is guarded by a digest of the core state, so it cannot be
    copied verbatim: the fork rewrites ``state.json``, the digest stops matching,
    and ``_merge_resume`` drops the whole file with a warning nobody reads.

    ``fork_project`` also writes ``evaluator_ref`` into the core state from the
    baseline experiment it resolved; that stays as the fallback for a source
    that has no resume file at all.
    """
    source_resume = source_athena / "resume.json"
    if not source_resume.is_file():
        return
    try:
        payload = json.loads(source_resume.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    carried = {
        key: payload[key]
        for key in CARRIED_RESUME_FIELDS
        if payload.get(key) is not None
    }
    if not carried:
        return
    (target_athena / "resume.json").write_text(
        json.dumps(
            {"state_digest": _core_digest(target_core), **carried},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


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
    baseline_id, baseline = _baseline(tree)
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
    carried = _carry_eda_workspace(source_root, target_root, state.get("eda_dir"))
    if carried is not None:
        copied.append(carried)
    else:
        # 带不过来就把字段清掉，让 runtime 走"EDA 未捕获"那条明确的路，而不是指着
        # 一个不存在的目录让每个 lane 各抛一次异常。
        state["eda_dir"] = None
    state["evaluator_ref"] = evaluator_ref
    state.update(RESET_STATE_FIELDS)
    (target_athena / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _carry_resume_fields(source_athena, target_athena, state)
    return ForkResult(
        source=source_root,
        target=target_root,
        evaluator_ref=evaluator_ref,
        baseline_experiment_id=baseline_id,
        copied=tuple(copied),
    )
