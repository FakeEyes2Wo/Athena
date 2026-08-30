"""SEARCH 与 FINAL 两个 evaluator 之间的隔离。

真机（2026-08-29，qwen3.7-plus 跑 TESS 恒星耀发任务）：两个 evaluator 共用
agent_type ``"evaluator"``，而工厂在注册时就把 workspace 绑死了。``_run_evaluator_agent``
里那句 ``if not registry.contains("evaluator")`` 意味着**第二个 evaluator 复用了第一个
的注册**——于是 FINAL evaluator 的 ``read_file``/``write_file`` 仍然指向 SEARCH
evaluator 的目录。

后果有两层，第二层才是要命的：

1. FINAL evaluator 自己的目录一个文件都没有，``_evaluator_layout`` 每次都因
   ``metric.json is missing`` 拒绝，agent 连交 10 次 submit 直到轮次预算耗尽；
2. 它那句"把 labels.csv 写进我的工作区"落进了**已冻结的 SEARCH evaluator**，
   把后者的标签换成了留出集的标签。SEARCH 于是会在 VALIDATE 要用的那批行上打分，
   而分数看起来完全正常——没有任何一层会报错。
"""

from pathlib import Path

import pytest

from athena.research.prepare_phase import _assert_evaluator_splits_are_disjoint


def _labels(path: Path, row_ids: range) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["__athena_row_id,label"]
    lines += [f"{i},{i % 2}" for i in row_ids]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_disjoint_evaluator_splits_are_accepted(tmp_path: Path) -> None:
    search = _labels(tmp_path / "evaluator" / "labels.csv", range(0, 100))
    final = _labels(tmp_path / "final_evaluator" / "labels.csv", range(100, 200))

    _assert_evaluator_splits_are_disjoint(search, final)


def test_the_held_out_split_leaking_into_search_is_caught(tmp_path: Path) -> None:
    """这正是真机上发生的形状：SEARCH 的 labels 被换成了 FINAL 的那一批行。"""
    search = _labels(tmp_path / "evaluator" / "labels.csv", range(100, 200))
    final = _labels(tmp_path / "final_evaluator" / "labels.csv", range(100, 200))

    with pytest.raises(RuntimeError, match="not held out"):
        _assert_evaluator_splits_are_disjoint(search, final)


def test_partial_overlap_is_also_caught(tmp_path: Path) -> None:
    search = _labels(tmp_path / "evaluator" / "labels.csv", range(0, 120))
    final = _labels(tmp_path / "final_evaluator" / "labels.csv", range(100, 200))

    with pytest.raises(RuntimeError, match="overlapping rows"):
        _assert_evaluator_splits_are_disjoint(search, final)


def test_a_missing_labels_file_is_left_to_the_freeze_step(tmp_path: Path) -> None:
    """标签文件缺失是冻结步骤的问题；别把它变成一条让人误解的隔离错误。"""
    search = _labels(tmp_path / "evaluator" / "labels.csv", range(0, 100))

    _assert_evaluator_splits_are_disjoint(search, tmp_path / "nope" / "labels.csv")


@pytest.mark.asyncio
async def test_each_evaluator_agent_is_bound_to_its_own_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    """第二个 evaluator 必须拿到自己的 workspace，而不是继续用第一个的。"""
    from types import SimpleNamespace

    import athena.research.prepare_phase as phase

    bound: list[Path] = []

    class Registry:
        def __init__(self) -> None:
            self._types: set[str] = set()

        def contains(self, agent_type: str) -> bool:
            return agent_type in self._types

        def unregister(self, agent_type: str) -> None:
            self._types.discard(agent_type)

    registry = Registry()

    def fake_register(reg, *, workspace, **kwargs):
        if reg.contains("evaluator"):
            raise ValueError("agent_type already registered: evaluator")
        reg._types.add("evaluator")
        bound.append(Path(workspace))

    async def fake_plan(**kwargs):
        return "sha256:" + "a" * 64

    monkeypatch.setattr(phase, "register_evaluator_agent", fake_register)
    monkeypatch.setattr(phase, "run_evaluator_plan", fake_plan)

    rt = SimpleNamespace(
        workspaces_root=tmp_path / "workspaces",
        registry=registry,
        provider=object(),
        store=object(),
        execution=object(),
        agents=object(),
        scripts=object(),
        events=SimpleNamespace(project_agent_event=lambda *a: None),
        kaggle_tools=lambda name: None,
    )

    for directory in ("evaluator", "final_evaluator"):
        await phase._run_evaluator_agent(
            rt,
            directory_name=directory,
            agent_id=directory,
            plan_id=directory,
            task="build it",
            label=directory,
        )

    assert bound == [
        tmp_path / "workspaces" / "evaluator",
        tmp_path / "workspaces" / "final_evaluator",
    ]
