"""ResearchRuntime 公开门面测试（supervisor_design §3 控制面）。

ResearchRuntime 是 Supervisor 子系统的唯一公开门面；dispatch 只暴露新的控制面
方法（PARSE_INTENT/TASK_CONFIGURE/RUN/PAUSE/RESUME/STOP/STATUS/...），不再有
SEARCH_START/VALIDATE_START 等阶段推进命令。
"""

from pathlib import Path

import pytest

from athena.research import ResearchMethod, ResearchRuntime
from test.unit._support import make_project


def _task_params(data_path: str = "") -> dict[str, object]:
    return {
        "task_type": "classification",
        "data_type": "tabular",
        "target_vars": ["label"],
        "primary_metric": "f1_macro",
        "direction": "maximize",
        "data_path": data_path,
        "target": "label",
    }


async def _make_runtime(tmp_path: Path) -> ResearchRuntime:
    runtime = make_project(tmp_path)
    return runtime


@pytest.mark.asyncio
async def test_publish_isolates_failing_subscriber(tmp_path) -> None:
    """事件 fan-out 的错误边界：一个 subscriber 抛异常不影响其他 subscriber（不静默吞掉发布）。"""
    runtime = await _make_runtime(tmp_path)
    received: list[str] = []
    runtime.subscribe(lambda kind, data: (_ for _ in ()).throw(RuntimeError("boom")))
    runtime.subscribe(lambda kind, data: received.append(kind))
    await runtime._publish("some/event", {"n": 1})  # 内部发布路径不抛、不中断 fan-out
    assert received == ["some/event"]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_parse_intent(tmp_path) -> None:
    """PARSE_INTENT 不触碰项目状态，只原样返回意图文本。"""
    runtime = await _make_runtime(tmp_path)
    parsed = await runtime.dispatch(
        ResearchMethod.PARSE_INTENT,
        {"message": "classify tabular data, optimize f1"},
    )
    assert parsed["intent"] == "classify tabular data, optimize f1"
    assert parsed["needs_configuration"] is True
    await runtime.aclose()


@pytest.mark.asyncio
async def test_status_without_execution_returns_idle(tmp_path) -> None:
    """STATUS 不隐式创建 execution；无 execution → execution=None, phase=IDLE。"""
    runtime = await _make_runtime(tmp_path)
    status = await runtime.dispatch(ResearchMethod.STATUS, {})
    assert status["execution"] is None
    assert status["phase"] == "IDLE"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_run_preserves_auto_mode_and_registers_workers(tmp_path) -> None:
    """TASK_CONFIGURE 保存 interaction_mode；RUN 保留它并注册 worker 类型。"""
    runtime = await _make_runtime(tmp_path)
    await runtime.dispatch(
        ResearchMethod.TASK_CONFIGURE,
        {
            "interaction_mode": "auto",
            "task": "predict an outcome",
            "data_path": "data",
        },
    )
    result = await runtime.dispatch(ResearchMethod.RUN, {})
    assert result["interaction_mode"] == "auto"
    assert {"init", "data", "reflection", "ideator", "code"} <= set(
        runtime.registered_worker_types
    )
    await runtime.aclose()


@pytest.mark.asyncio
async def test_task_configure_projects_prepare(tmp_path) -> None:
    """TASK_CONFIGURE 提交 task 事实 → 阶段投影 PREPARE。"""
    runtime = await _make_runtime(tmp_path)
    result = await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, _task_params())
    assert result["configured"] is True
    assert result["phase"] == "PREPARE"
    assert runtime.phase == "PREPARE"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_task_configure_resolves_relative_data_path(tmp_path) -> None:
    """相对 data_path 在 TASK_CONFIGURE 解析为绝对路径（worker 独立 workspace 可读）。"""
    runtime = await _make_runtime(tmp_path)
    await runtime.dispatch(
        ResearchMethod.TASK_CONFIGURE,
        {
            "task_type": "classification",
            "data_type": "tabular",
            "target_vars": ["label"],
            "primary_metric": "accuracy",
            "direction": "maximize",
            "data_path": "some/relative/data",
            "target": "label",
        },
    )
    cfg = runtime.journal.get_fact("task_config")
    assert cfg is not None and isinstance(cfg, dict)
    resolved = cfg["data_payload"]["data_path"]
    assert isinstance(resolved, str) and Path(resolved).is_absolute()
    assert Path(resolved).as_posix().endswith("some/relative/data")
    await runtime.aclose()


@pytest.mark.asyncio
async def test_run_returns_execution_and_starts_coordinator(tmp_path) -> None:
    """RUN 幂等创建活动 execution 并立即返回（后台 Coordinator 持续推进）。"""
    runtime = await _make_runtime(tmp_path)
    # 有效 data_path，避免后台 coordinator 因 ingest 缺源而把 execution 置 FAILED
    await runtime.dispatch(
        ResearchMethod.TASK_CONFIGURE, _task_params(data_path=str(_dataset(tmp_path)))
    )
    run = await runtime.dispatch(ResearchMethod.RUN, {})
    assert run["status"] == "running"
    assert run["execution_id"].startswith("exec_")
    # 同一项目最多一个活动 execution：再次 RUN 复用同一 id
    run2 = await runtime.dispatch(ResearchMethod.RUN, {})
    assert run2["execution_id"] == run["execution_id"]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_failed_plan_projects_failed_status(tmp_path) -> None:
    """无效 data_path → ingest 计划失败 → STATUS 显示 FAILED（不永久轮询）。"""
    runtime = make_project(tmp_path)
    await runtime.dispatch(
        ResearchMethod.TASK_CONFIGURE,
        _task_params(data_path=str(tmp_path / "nope.csv")),
    )
    await runtime.dispatch(ResearchMethod.RUN, {})
    assert await _eventually(
        lambda: runtime.project_status() == "FAILED", timeout=30
    ), f"status stuck at {runtime.project_status()}"
    status = await runtime.dispatch(ResearchMethod.STATUS, {})
    assert status["execution"]["status"] == "FAILED"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_run_after_stop_creates_new_execution(tmp_path) -> None:
    """STOP（CANCELLED）后再次 RUN 创建新 execution；旧保持 terminal。"""
    runtime = await _make_runtime(tmp_path)
    await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, _task_params())
    first = await runtime.dispatch(ResearchMethod.RUN, {})
    await runtime.dispatch(ResearchMethod.STOP, {})
    second = await runtime.dispatch(ResearchMethod.RUN, {})
    assert second["execution_id"] != first["execution_id"]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_status_explicit_execution_id_after_stop(tmp_path) -> None:
    """STATUS 可用 execution_id 显式选择已 terminal 的 execution。"""
    runtime = await _make_runtime(tmp_path)
    await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, _task_params())
    run = await runtime.dispatch(ResearchMethod.RUN, {})
    execution_id = run["execution_id"]
    await runtime.dispatch(ResearchMethod.STOP, {})
    status = await runtime.dispatch(
        ResearchMethod.STATUS, {"execution_id": execution_id}
    )
    assert status["execution"]["id"] == execution_id
    assert status["execution"]["status"] == "CANCELLED"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_status_snapshot_shape(tmp_path) -> None:
    """STATUS 返回紧凑快照；不存在的可选对象用 null，不省略字段。"""
    runtime = await _make_runtime(tmp_path)
    await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, _task_params())
    status = await runtime.dispatch(ResearchMethod.STATUS, {})
    assert status["execution"]["phase"] == "PREPARE"
    assert status["execution"]["status"] == "RUNNING"
    assert "state_version" in status
    assert status["human_request"] is None
    assert status["error"] is None
    assert status["budgets"]["plans_remaining"] == 100
    await runtime.aclose()


@pytest.mark.asyncio
async def test_pause_resume_stop(tmp_path) -> None:
    """控制状态正交：PAUSE/RESUME/STOP 只改控制状态，不改研究阶段。"""
    runtime = await _make_runtime(tmp_path)
    await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, _task_params())
    paused = await runtime.dispatch(ResearchMethod.PAUSE, {})
    assert paused["status"] == "paused"
    assert runtime.project_status() == "PAUSED"
    resumed = await runtime.dispatch(ResearchMethod.RESUME, {})
    assert resumed["status"] == "running"
    stopped = await runtime.dispatch(ResearchMethod.STOP, {})
    assert stopped["status"] == "stopped"
    assert runtime.project_status() == "CANCELLED"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_unknown_method_rejected(tmp_path) -> None:
    """旧阶段推进命令不再存在；未知方法报错。"""
    runtime = await _make_runtime(tmp_path)
    with pytest.raises(ValueError, match="unknown method"):
        await runtime.dispatch("SEARCH_START", {})
    await runtime.aclose()


@pytest.mark.asyncio
async def test_tree_get_save_load(tmp_path) -> None:
    """TREE_GET/SAVE/LOAD 走 ResearchTree 快照。"""
    runtime = await _make_runtime(tmp_path)
    tree = await runtime.dispatch(ResearchMethod.TREE_GET, {})
    assert tree["tree"]["version"] == 2
    saved = await runtime.dispatch(
        ResearchMethod.TREE_SAVE, {"path": str(tmp_path / "tree.json")}
    )
    assert saved["saved"] is True
    loaded = await runtime.dispatch(
        ResearchMethod.TREE_LOAD, {"path": str(tmp_path / "tree.json")}
    )
    assert loaded["loaded"] is True
    await runtime.aclose()


# ---- 迁移自 test/unit/test_project_runtime.py（supervisor_imp_docs Task 10）----
# 仍有效的 ResearchRuntime 集成行为：协调循环推进、跨重启恢复、worker 工厂。

import json

import pandas as pd


def _dataset(tmp_path: Path) -> Path:
    """写一个小型 CSV，供 DataAgent 脚本分析。"""
    path = tmp_path / "dataset.csv"
    pd.DataFrame({"age": range(20), "income": range(20), "label": [0, 1] * 10}).to_csv(
        path, index=False
    )
    return path


async def _configure(runtime: ResearchRuntime, tmp_path: Path) -> None:
    """TASK_CONFIGURE：提交 task 事实 + task_config（typed task）。"""
    await runtime.dispatch(
        "TASK_CONFIGURE",
        {
            "task_type": "classification",
            "data_type": "tabular",
            "target_vars": ["label"],
            "primary_metric": "accuracy",
            "direction": "maximize",
            "data_path": str(_dataset(tmp_path)),
            "target": "label",
        },
    )


async def _eventually(pred, timeout: float = 30) -> bool:
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    while not pred():
        if asyncio.get_event_loop().time() > deadline:
            return False
        await asyncio.sleep(0)
    return True


def _unfinished_plan_count(runtime: ResearchRuntime) -> int:
    execution_id = runtime.coordinator.execution_id
    if execution_id is None:
        return 0
    return 1 if runtime.journal.load_unfinished_plan(execution_id) is not None else 0


@pytest.mark.asyncio
async def test_registers_six_workers_no_supervisor(tmp_path) -> None:
    """supervisor 不再是 registry 类型：只注册 init/data/plot/reflection/ideator/code。"""
    runtime = make_project(tmp_path)
    assert set(runtime.kernel._registry.types) == {
        "init",
        "data",
        "plot",
        "reflection",
        "ideator",
        "code",
    }
    assert "supervisor" not in runtime.kernel._registry.types
    await runtime.aclose()


@pytest.mark.asyncio
async def test_coordinator_drives_to_completed(tmp_path) -> None:
    """RUN 启动后台 Coordinator，从 PREPARE 推进到 COMPLETED（事实驱动）。"""
    runtime = make_project(tmp_path)
    await _configure(runtime, tmp_path)
    budget = runtime.state.budget()
    budget.max_search_experiments = 1
    runtime.state.save_budget(budget)
    run = await runtime.dispatch("RUN", {})
    assert run["status"] == "running"
    assert await _eventually(
        lambda: runtime.phase == "COMPLETED", timeout=30
    ), f"phase stuck at {runtime.phase}"
    facts = runtime.state.facts()
    assert facts.dataset_role_review_ref is not None
    assert facts.eval_spec_ref is not None
    assert facts.baseline_experiment_ref is not None
    assert facts.sota_experiment_ref is not None
    assert facts.search_stop_ref is not None
    assert facts.validation_result_ref is not None
    await runtime.aclose()


@pytest.mark.asyncio
async def test_phase_refs_survive_reopen(tmp_path) -> None:
    """阶段事实跨重启恢复：重新打开项目后投影仍为 COMPLETED。"""
    runtime = make_project(tmp_path)
    await _configure(runtime, tmp_path)
    budget = runtime.state.budget()
    budget.max_search_experiments = 1
    runtime.state.save_budget(budget)
    await runtime.dispatch("RUN", {})
    assert await _eventually(lambda: runtime.phase == "COMPLETED", timeout=30)
    await runtime.aclose()

    reopened = make_project(tmp_path)
    assert reopened.phase == "COMPLETED"
    assert reopened.state.facts().validation_result_ref is not None
    await reopened.aclose()


@pytest.mark.asyncio
async def test_status_and_budget_survive_reopen(tmp_path) -> None:
    """STATUS 快照与预算跨重启恢复。"""
    runtime = make_project(tmp_path)
    await _configure(runtime, tmp_path)
    budget = runtime.state.budget()
    budget.max_search_experiments = 1
    runtime.state.save_budget(budget)
    await runtime.dispatch("RUN", {})
    assert await _eventually(lambda: runtime.phase == "COMPLETED", timeout=30)
    status = await runtime.dispatch("STATUS", {})
    assert status["budgets"]["plans_remaining"] >= 0
    assert status["execution"]["phase"] == "COMPLETED"
    await runtime.aclose()

    reopened = make_project(tmp_path)
    status2 = await reopened.dispatch("STATUS", {})
    assert status2["execution"]["phase"] == "COMPLETED"
    await reopened.aclose()


@pytest.mark.asyncio
async def test_reopen_reuses_same_execution(tmp_path) -> None:
    """重新打开项目复用同一活动 execution。"""
    runtime = make_project(tmp_path)
    await _configure(runtime, tmp_path)
    run = await runtime.dispatch("RUN", {})
    execution_id = run["execution_id"]
    await runtime.aclose()

    reopened = make_project(tmp_path)
    status = await reopened.dispatch("STATUS", {})
    assert status["execution"]["id"] == execution_id
    await reopened.aclose()


@pytest.mark.asyncio
async def test_workers_are_real_factories(tmp_path) -> None:
    """worker 为真实 factory，产出真实结果 Artifact。"""
    runtime = make_project(tmp_path)
    for agent_type, (content, key) in {
        "ideator": ("新假设", "hypothesis"),
        "code": ("补丁", "diff"),
    }.items():
        _, run_id = await runtime.kernel.create_root(
            agent_type, {"content": content}, name=f"{agent_type}-root"
        )
        summary = await runtime.kernel.wait_run(run_id, timeout=2)
        response = json.loads(summary.response_ref)
        result_ref = response["result_ref"]
        assert result_ref.startswith("sha256:")
        artifact = json.loads(await runtime.store.get_text(result_ref))
        assert artifact[key] == content
    await runtime.aclose()


@pytest.mark.asyncio
async def test_pause_stops_coordinator_then_resume(tmp_path) -> None:
    """PAUSE 后 Coordinator 不生成新 Plan；RESUME 后恢复推进。"""
    runtime = make_project(tmp_path)
    await _configure(runtime, tmp_path)
    await runtime.dispatch("PAUSE", {})
    assert runtime.project_status() == "PAUSED"
    assert _unfinished_plan_count(runtime) == 0
    await runtime.dispatch("RESUME", {})
    assert runtime.project_status() == "RUNNING"
    await runtime.dispatch("RUN", {})
    assert runtime.phase in ("PREPARE", "SEARCH", "VALIDATE", "COMPLETED")
    await runtime.aclose()


@pytest.mark.asyncio
async def test_stop_projects_cancelled(tmp_path) -> None:
    """STOP 投影 CANCELLED；保留已提交事实。"""
    runtime = make_project(tmp_path)
    await _configure(runtime, tmp_path)
    await runtime.dispatch("STOP", {})
    assert runtime.project_status() == "CANCELLED"
    assert runtime.state.facts().task_ref is not None
    await runtime.aclose()


@pytest.mark.asyncio
async def test_project_phase_projection_from_facts(tmp_path) -> None:
    """阶段投影完全由已提交事实派生（IDLE→PREPARE→SEARCH）。"""
    runtime = make_project(tmp_path)
    assert runtime.phase == "IDLE"
    await _configure(runtime, tmp_path)
    assert runtime.phase == "PREPARE"
    runtime.state.commit_facts(
        {
            "dataset_role_review_ref": "sha256:d",
            "dataset_manifest_ref": "sha256:m",
            "eval_spec_ref": "sha256:e",
            "eda_review_ref": "sha256:r",
            "baseline_experiment_ref": "sha256:b",
        }
    )
    assert runtime.phase == "SEARCH"
    await runtime.aclose()
