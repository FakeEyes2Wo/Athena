"""配置的 manifest 超时必须真的从 ResearchState 走到 PlanRunner。

``test_experiment.py`` 里的两个用例守的是 ``PlanRunner`` 自己的默认值与透传；
它们对「常量还在、但 ``phase_runner`` 那行接线被删掉」这种改法是瞎的——删掉之后
``PlanRunner`` 退回自己的长默认值，行为看起来照样正常，只是**项目配置从此无效**。
这一组用例守的就是那根线：断言 ``PlanRunner`` 收到的是 ``state.experiment_timeout_s``
本身，而不是任何默认值。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.research import phase_runner as phase_runner_module
from athena.research.phase_runner import PhaseRunner
from athena.research.supervisor.plans import DEFAULT_EXPERIMENT_TIMEOUT_S
from athena.research.supervisor.plans import PlanInput, PlanState

_REF = "sha256:" + "a" * 64


def _fake_runtime(tmp_path: Path, *, timeout_s: int) -> SimpleNamespace:
    """够 ``run_plan_turn`` 跑到构造 ``PlanRunner`` 那一步的最小替身。"""

    def unavailable_plan_turn(plan_id, state):  # pragma: no cover - 名字才是契约
        raise AssertionError("not used")

    branch = SimpleNamespace(path=str(tmp_path / "ws"), branch="b", base_commit="c0")

    async def plan_input(plan_id: str) -> PlanInput:
        return PlanInput(evaluator_ref=_REF, tree_ref=_REF)

    execution = object()

    async def execution_for(plan_id: str, workspace):
        return execution

    return SimpleNamespace(
        _plan_turn=unavailable_plan_turn,
        _root=tmp_path,
        _execution=execution,
        execution_for=execution_for,
        placement_for=lambda plan_id: None,
        _store=object(),
        _evaluator=object(),
        _git=object(),
        state=SimpleNamespace(experiment_timeout_s=timeout_s),
        _supervisor=SimpleNamespace(
            plan_input=plan_input,
            workspace=lambda plan_id: branch,
            workspace_path=lambda plan_id: tmp_path / "ws",
        ),
    )


class _Recorder:
    """截住 PlanRunner 的构造参数，run_turn 直接返回。"""

    seen: dict = {}

    def __init__(self, **kwargs) -> None:
        _Recorder.seen = kwargs

    async def run_turn(self, plan_id, state, plan_input, *, emit=None):
        return "done"


@pytest.mark.asyncio
async def test_plan_turn_uses_the_projects_configured_timeout(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(phase_runner_module, "PlanRunner", _Recorder)
    runtime = _fake_runtime(tmp_path, timeout_s=5400)

    state = PlanState(
        kind="SEARCH", context_ref=_REF, turns_used=0, turn_limit=12, patience=4
    )
    assert await PhaseRunner(runtime).run_plan_turn("h1", state) == "done"

    # 5400 既不是 PlanRunner 的默认值也不是任何字面量，只可能来自 state。
    assert _Recorder.seen["timeout_s"] == 5400
    assert _Recorder.seen["timeout_s"] != DEFAULT_EXPERIMENT_TIMEOUT_S
