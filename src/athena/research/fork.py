"""Fork PREPARE outputs into an isolated SEARCH experiment arm.

The fork copies the frozen evaluator artifacts, baseline repository, and EDA inputs
so arms share one starting point while keeping independent Supervisor state.
"""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from athena.research.supervisor.state import _core_digest

_FORKED_DIRECTORIES = ("artifacts", "repo")

CARRIED_RESUME_FIELDS = (
    "task_text",
    "data_contract",
    "evaluator_ref",
    "final_evaluator_ref",
    "kaggle_download",
)
"""PREPARE 的产物里存在 ``resume.json`` 而非 ``state.json`` 的那几项。

分叉曾经只复制 ``state.json``，而这些字段写在兄弟文件 ``resume.json`` 里，新臂开跑时
它们全是 ``None``：``data_contract`` 为空时 SEARCH 候选拿不到数据契约，会自己去数据目录
里找划分并在被打分的行上训练；``final_evaluator_ref`` 为空时 VALIDATE 退回搜索
evaluator，拿 search 标签当留出集打分。两者都只在分数上表现出来，且都是更好看的方向。

``task_research_*`` 刻意不带：与 ``corpus_ref`` 同类，属于要被比较的那个变量。
"""


class ForkError(RuntimeError):
    """源项目不具备分叉条件，或目标已存在。"""


@dataclass(frozen=True, slots=True)
class ForkResult:
    """一次分叉的结果，供 CLI 打印与用例断言。"""

    evaluator_ref: str
    baseline_experiment_id: str
    copied: tuple[str, ...]


def _baseline(tree: dict) -> tuple[str, dict]:
    """Return the canonical baseline experiment key and payload."""
    experiments = tree.get("experiments")
    if not isinstance(experiments, dict):
        experiments = {}
    for experiment_id, experiment in experiments.items():
        if not isinstance(experiment, dict):
            continue
        plan = experiment.get("plan")
        kind = plan.get("kind") if isinstance(plan, dict) else None
        if kind == "baseline":
            return str(experiment_id), experiment
    raise ForkError(
        "source project has no baseline experiment; run PREPARE there first — "
        "forking exists precisely to avoid re-running it per arm"
    )


def _carry_eda_workspace(
    source_root: Path, target_root: Path, eda_dir: object
) -> str | None:
    """Copy a safe relative EDA workspace and remove its source worktree link."""
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
    (destination / ".git").unlink(missing_ok=True)
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
    """Copy ``source`` PREPARE outputs into a fresh isolated SEARCH arm."""
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
    plan = baseline.get("plan")
    evaluator_ref = plan.get("run_config_ref") if isinstance(plan, dict) else None
    if not isinstance(evaluator_ref, str) or not evaluator_ref:
        raise ForkError(
            "baseline experiment carries no frozen evaluator reference; the source "
            "project's PREPARE did not complete"
        )

    target_athena.mkdir(parents=True)
    copied: list[str] = []
    for name in _FORKED_DIRECTORIES:
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
    state.update(
        status="RUNNING",
        phase="SEARCH",
        plans={},
        validation=None,
        corpus_ref=None,
        corpus_ideated_ref=None,
    )
    (target_athena / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _carry_resume_fields(source_athena, target_athena, state)
    return ForkResult(
        evaluator_ref=evaluator_ref,
        baseline_experiment_id=baseline_id,
        copied=tuple(copied),
    )
