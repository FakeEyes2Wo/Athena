"""PREPARE 步骤级续跑：已完成的步骤不再重跑。

``run_prepare_phase`` 有两个串行步骤，各自是一整轮多回合 agent：先冻结 evaluator，
再跑 baseline 并可信打分。续跑时若 evaluator 已冻结（引用在 state 的 PREPARE 断点
里且能从 artifact store 解析出来），必须直接进第二步，不能把第一步整个重来。
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_tree import ResearchTree
from athena.research import phase_runner as phase_runner_module
from athena.research.phase_runner import PhaseRunner
from athena.research.supervisor.prepare import PrepareResult
from athena.research.supervisor.state import ResearchState


class _Workspace:
    def __init__(self, path: Path) -> None:
        self.path = str(path)


class _Git:
    """GitWorkspace 替身：记录 create 调用次数，好断言 worktree 没被重建。"""

    def __init__(self, workspace_path: Path) -> None:
        self.created: list[str] = []
        self._workspace_path = workspace_path

    async def init(self, **_kwargs: Any) -> str:
        return "commit0"

    async def create(self, _commit: str, branch: str, name: str | None = None) -> _Workspace:
        self.created.append(branch)
        self._workspace_path.mkdir(parents=True, exist_ok=True)
        return _Workspace(self._workspace_path)


def _runtime(tmp_path: Path, *, prepare: dict[str, object] | None, eda_dir: str | None):
    """A PhaseRunner host stubbed down to what ``run_prepare_phase`` touches."""
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    state = ResearchState(
        status="RUNNING", phase="PREPARE", search_limit=10, concurrency=1
    )
    state.prepare = prepare
    state.eda_dir = eda_dir
    checkpoints: list[dict[str, object]] = []

    async def checkpoint_prepare(**fields: object) -> None:
        checkpoints.append(dict(fields))
        state.prepare = {**(state.prepare or {}), **fields}

    async def publish_output(**_payload: Any) -> None:
        return None

    runtime = SimpleNamespace(
        _root=root,
        _prepare_phase=None,
        _provider=object(),
        _git=_Git(root / "workspaces" / "eda"),
        _state=state,
        _state_path=tmp_path / "state.json",
        _workspaces_root=root / "workspaces",
        _registry=SimpleNamespace(contains=lambda _name: True),
        _store=LocalArtifactStore(tmp_path / "artifacts"),
        _agents=object(),
        _scripts=object(),
        _evaluator=object(),
        _execution=SimpleNamespace(ensure_environment=lambda: None),
        _task_text="task",
        _events_bus=SimpleNamespace(project_agent_event=lambda *a, **k: None),
        _supervisor=SimpleNamespace(checkpoint_prepare=checkpoint_prepare),
        tree=ResearchTree(),
        publish_output=publish_output,
        kaggle_tools=lambda _agent_type: None,
        state=state,
    )
    runtime.checkpoints = checkpoints
    return runtime


def _stub_prepare_plan(monkeypatch, seen: dict[str, Any]) -> None:
    """Replace step 2 with a recorder returning a trusted baseline."""

    async def fake_run_prepare_plan(**kwargs: Any) -> PrepareResult:
        seen["evaluator_ref"] = kwargs["evaluator_ref"]
        seen["turns_used"] = kwargs.get("turns_used")
        return PrepareResult(
            evaluator_ref=kwargs["evaluator_ref"],
            metric=0.8,
            commit="c1",
            predictions_ref="p1",
            evidence_ref="e1",
            report_ref="r1",
        )

    monkeypatch.setattr(
        phase_runner_module, "run_prepare_plan", fake_run_prepare_plan
    )


@pytest.mark.asyncio
async def test_resume_skips_the_already_frozen_evaluator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """断点里记着一个可解析的 evaluator_ref → 第一步整个跳过。"""
    runtime = _runtime(tmp_path, prepare=None, eda_dir=None)
    evaluator_ref = await runtime._store.put_text(json.dumps({"frozen": True}))
    runtime._state.prepare = {"evaluator_ref": evaluator_ref}

    async def fail_evaluator(**_kwargs: Any) -> str:
        raise AssertionError("evaluator step must not run again after a checkpoint")

    monkeypatch.setattr(phase_runner_module, "run_evaluator_plan", fail_evaluator)
    seen: dict[str, Any] = {}
    _stub_prepare_plan(monkeypatch, seen)

    result = await PhaseRunner(runtime).run_prepare_phase()

    assert seen["evaluator_ref"] == evaluator_ref
    assert result.evaluator_ref == evaluator_ref


@pytest.mark.asyncio
async def test_fresh_run_freezes_the_evaluator_and_checkpoints_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有断点 → 照常冻结 evaluator，并把引用记进 PREPARE 断点。"""
    runtime = _runtime(tmp_path, prepare=None, eda_dir=None)
    frozen_ref = await runtime._store.put_text(json.dumps({"frozen": True}))

    async def fake_evaluator(**_kwargs: Any) -> str:
        return frozen_ref

    monkeypatch.setattr(phase_runner_module, "run_evaluator_plan", fake_evaluator)
    seen: dict[str, Any] = {}
    _stub_prepare_plan(monkeypatch, seen)

    await PhaseRunner(runtime).run_prepare_phase()

    assert {"evaluator_ref": frozen_ref} in runtime.checkpoints


@pytest.mark.asyncio
async def test_baseline_step_resumes_from_the_checkpointed_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """断点记着 baseline 已用 2 轮 → 第二步从第 2 轮接着跑。"""
    runtime = _runtime(tmp_path, prepare=None, eda_dir=None)
    evaluator_ref = await runtime._store.put_text(json.dumps({"frozen": True}))
    runtime._state.prepare = {"evaluator_ref": evaluator_ref, "prepare_turns": 2}

    async def unused_evaluator(**_kwargs: Any) -> str:
        raise AssertionError("evaluator step must not run again")

    monkeypatch.setattr(phase_runner_module, "run_evaluator_plan", unused_evaluator)
    seen: dict[str, Any] = {}
    _stub_prepare_plan(monkeypatch, seen)

    await PhaseRunner(runtime).run_prepare_phase()

    assert seen["turns_used"] == 2


@pytest.mark.asyncio
async def test_evaluator_step_resumes_from_the_checkpointed_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """evaluator 冻结到一半断掉 → 第一步从记下的轮次接着跑，不重置预算。"""
    runtime = _runtime(tmp_path, prepare={"evaluator_turns": 1}, eda_dir=None)
    frozen_ref = await runtime._store.put_text(json.dumps({"frozen": True}))
    seen_evaluator: dict[str, Any] = {}

    async def fake_evaluator(**kwargs: Any) -> str:
        seen_evaluator["turns_used"] = kwargs.get("turns_used")
        return frozen_ref

    monkeypatch.setattr(phase_runner_module, "run_evaluator_plan", fake_evaluator)
    seen: dict[str, Any] = {}
    _stub_prepare_plan(monkeypatch, seen)

    await PhaseRunner(runtime).run_prepare_phase()

    assert seen_evaluator["turns_used"] == 1


@pytest.mark.asyncio
async def test_each_step_checkpoints_its_own_turn_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个步骤各记各的轮次，不能互相覆盖。"""
    runtime = _runtime(tmp_path, prepare=None, eda_dir=None)
    frozen_ref = await runtime._store.put_text(json.dumps({"frozen": True}))

    async def fake_evaluator(**kwargs: Any) -> str:
        await kwargs["checkpoint"](1)
        return frozen_ref

    async def fake_prepare(**kwargs: Any) -> PrepareResult:
        await kwargs["checkpoint"](3)
        return PrepareResult(
            evaluator_ref=kwargs["evaluator_ref"],
            metric=0.8,
            commit="c1",
            predictions_ref="p1",
            evidence_ref="e1",
            report_ref="r1",
        )

    monkeypatch.setattr(phase_runner_module, "run_evaluator_plan", fake_evaluator)
    monkeypatch.setattr(phase_runner_module, "run_prepare_plan", fake_prepare)

    await PhaseRunner(runtime).run_prepare_phase()

    assert runtime._state.prepare["evaluator_turns"] == 1
    assert runtime._state.prepare["prepare_turns"] == 3


@pytest.mark.asyncio
async def test_stale_evaluator_ref_falls_back_to_refreezing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """断点里的引用在 store 里取不到（换机器/清过缓存）→ 老实重冻，不能直接崩。"""
    runtime = _runtime(
        tmp_path, prepare={"evaluator_ref": "sha256:missing"}, eda_dir=None
    )
    frozen_ref = await runtime._store.put_text(json.dumps({"frozen": True}))
    calls: list[str] = []

    async def fake_evaluator(**_kwargs: Any) -> str:
        calls.append("froze")
        return frozen_ref

    monkeypatch.setattr(phase_runner_module, "run_evaluator_plan", fake_evaluator)
    seen: dict[str, Any] = {}
    _stub_prepare_plan(monkeypatch, seen)

    await PhaseRunner(runtime).run_prepare_phase()

    assert calls == ["froze"]
    assert seen["evaluator_ref"] == frozen_ref
