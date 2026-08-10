"""Supervisor 核心组件单元测试（supervisor_design §5-§8）。

覆盖：PlanJournal SQLite 持久化与 CAS、阶段投影、Planner 状态机、Validator
白名单/预算/idempotency、Executor 的 spawn→wait→commit 闭环，以及 Coordinator
从 TASK_CONFIGURE 推进到 COMPLETED 的全流程。
"""

import json
from pathlib import Path
from typing import TypedDict

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_tree import ResearchTree
from athena.research.data_service import DatasetService
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import BundleMetadata, DataScriptRunner
from athena.research.search import SearchService
from athena.research.services import ResearchServices
from athena.research.supervisor.coordinator import SupervisorCoordinator
from athena.research.supervisor.executor import PlanExecutor
from athena.research.supervisor.journal import PlanJournal, VersionConflict
from athena.research.supervisor.models import (
    ControlStatus,
    OperationStatus,
    OperationType,
    PlanStatus,
    ResearchPhase,
    SupervisorOperation,
    SupervisorPlan,
)
from athena.research.supervisor.planner import DeterministicSupervisorPlanner
from athena.research.supervisor.state import (
    ProjectFacts,
    ProjectStateStore,
    project_phase,
)
from athena.research.supervisor.validator import PlanValidator, ValidationError


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# CodeAgent 确定性候选的 predictions/labels（与 _configure 冻结的 accuracy eval bundle 兼容）。
# baseline 预测更弱（accuracy 0.5），候选更强（accuracy 1.0），让 SEARCH 候选真实优于 baseline。
PREDICTIONS_CSV = "__athena_row_id,prediction\nr1,0.0\nr2,1.0\n"
PREDICTIONS_BASELINE_CSV = "__athena_row_id,prediction\nr1,0.0\nr2,0.0\n"
LABELS_CSV = "__athena_row_id,target\nr1,0.0\nr2,1.0\n"

# 最小 uv eval 项目：读 predictions.csv/labels.csv 算 accuracy，写 primary（stdlib-only）。
EVAL_SCRIPT = (
    "import csv, json, sys\n"
    "def main():\n"
    "    out = open(sys.argv[sys.argv.index('--output') + 1], 'w')\n"
    "    preds = {r[0]: float(r[1]) for r in list(csv.reader(open('predictions.csv')))[1:]}\n"
    "    labels = {r[0]: float(r[1]) for r in list(csv.reader(open('labels.csv')))[1:]}\n"
    "    rows = [(preds[k], labels[k]) for k in preds if k in labels]\n"
    "    acc = sum(1.0 for a, b in rows if a == b) / len(rows)\n"
    "    json.dump({'primary': acc, 'metric': 'accuracy'}, out)\n"
    "main()\n"
)


class _FakeStatus:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeRunResult:
    """最小 Run 终态：completed 时携带 JSON response_ref。"""

    status: object
    response_ref: str | None

    def __init__(self, result_ref: str | None, *, failed: bool = False) -> None:
        self.status = _FakeStatus("failed" if failed else "completed")
        self.response_ref = (
            json.dumps({"result_ref": result_ref}) if result_ref is not None else None
        )


class FakeWorkerRuntime:
    """Executor 依赖的最小派发假实现：spawn→完成→等待。

    worker 把结果 JSON 写入 store 后返回其 ref；init 的 payload 含
    ``eval_script``，供 executor 的 extract 路径读取；reflection 的 payload
    含 ``verdict``（``review_verdict`` 可覆盖为 REVISE），供评审闭环测试。
    """

    def __init__(self, store: LocalArtifactStore) -> None:
        self._store = store
        self.spawned: list[dict[str, object]] = []
        self._results: dict[str, FakeRunResult] = {}
        self._next = 0
        self.fail_next: bool = False
        self.review_verdict: str = "ACCEPT"
        # 按评审类型覆盖（role 默认 ACCEPT、EDA 单独 REVISE 的专项测试用）
        self.role_review_verdict: str | None = None
        self.eda_review_verdict: str | None = None
        self.next_payload: dict | None = None  # 覆盖下一次 spawn 的 worker payload
        self.eval_workspace: str | None = None  # PREPARE_EVAL 冻结的真实 uv 工作区

    async def create_root(
        self, agent_type: str, task: object, *, name: str = "root"
    ) -> tuple[str, str]:
        self._next += 1
        agent_id = f"agent_{self._next}"
        run_id = f"run_{self._next}"
        self.spawned.append(
            {"agent_type": agent_type, "name": name, "task": task, "run_id": run_id}
        )
        if self.next_payload is not None:
            payload = self.next_payload
            self.next_payload = None
        elif agent_type == "reflection":
            request_text = json.dumps(task, ensure_ascii=False)
            if "review the eda report" in request_text:
                verdict = self.eda_review_verdict or self.review_verdict
            else:
                verdict = self.role_review_verdict or self.review_verdict
            payload = {
                "decision": verdict,
                "issues": [],
                "required_changes": [],
                "evidence_refs": [],
            }
        elif agent_type == "init":
            payload = {
                "task_understanding": "TU",
                "eval_script": "print(1)",
                "eval_workspace": self.eval_workspace or "",
                "eval_metadata": {"entrypoint": "eval.py"},
            }
        elif agent_type == "ideator":
            payload = {
                "result": f"ok:{run_id}",
                "hypotheses": [
                    {
                        "statement": "hypothesis 1",
                        "intervention": "feature-a",
                        "expected_effect": "raise score",
                    },
                    {
                        "statement": "hypothesis 2",
                        "intervention": "model-b",
                        "expected_effect": "raise score",
                    },
                ],
            }
        elif agent_type == "code":
            # 真实 agent 的 input_text = payload 的 JSON（base_runner trigger.content）；
            # 假实现解析 task["content"] 以兼容 baseline 标记。
            try:
                inner = json.loads(str(task.get("content", "")))
                is_baseline = bool(inner.get("baseline"))
            except Exception:
                is_baseline = False
            preds = PREDICTIONS_BASELINE_CSV if is_baseline else PREDICTIONS_CSV
            payload = {
                "result": f"ok:{run_id}",
                "predictions": preds,
                "labels": LABELS_CSV,
                "candidates": [
                    {
                        "candidate_id": f"cand_{run_id}",
                        "test_score": 0.85,
                        "direction": "maximize",
                        "predictions": preds,
                        "labels": LABELS_CSV,
                    },
                ],
            }
        else:
            payload = {"result": f"ok:{run_id}"}
        result_ref = await self._store.put_text(json.dumps(payload, ensure_ascii=False))
        if self.fail_next:
            self._results[run_id] = FakeRunResult(None, failed=True)
            self.fail_next = False
        else:
            self._results[run_id] = FakeRunResult(result_ref)
        return agent_id, run_id

    async def wait_run(
        self, run_id: str, *, timeout: float | None = None
    ) -> FakeRunResult:
        return self._results.get(run_id, FakeRunResult(None))

    async def followup(self, agent_id: str, task: object) -> str:
        """follow-up run 同样产生可等待的结果（修订后的源 Artifact）。"""
        self._next += 1
        run_id = f"run_{self._next}"
        payload = {"result": f"revised:{run_id}"}
        result_ref = await self._store.put_text(json.dumps(payload, ensure_ascii=False))
        self._results[run_id] = FakeRunResult(result_ref)
        return run_id

    async def send_message(
        self,
        agent_id: str,
        content: str,
        context_refs: list[str] | None = None,
        *,
        source: str | None = None,
    ) -> None:
        pass

    def has_agent(self, agent_id: str) -> bool:
        return False  # 单测不触发恢复对账（fresh journal 无已派发身份）

    async def resume_agent(
        self, agent_id: str, *, agent_type: str, name: str | None = None
    ) -> None:
        return None

    def run_summary(self, run_id: str) -> FakeRunResult | None:
        return self._results.get(run_id)


class _Components(TypedDict):
    journal: PlanJournal
    state: ProjectStateStore
    execution_id: str
    store: LocalArtifactStore
    runtime: FakeWorkerRuntime
    planner: DeterministicSupervisorPlanner
    validator: PlanValidator
    executor: PlanExecutor
    coordinator: SupervisorCoordinator
    services: ResearchServices
    runner: DataScriptRunner
    tmp_path: Path


def _components(tmp_path: Path, *, interaction_mode: str = "auto") -> _Components:
    """装配一套独立的 supervisor 组件（不依赖 ResearchRuntime 门面）。"""
    journal = PlanJournal(tmp_path / ".athena" / "supervisor.db")
    state = ProjectStateStore(journal)
    execution = journal.create_execution(interaction_mode=interaction_mode)
    execution_id = execution["execution_id"]
    store = LocalArtifactStore(tmp_path / "artifacts")
    runtime = FakeWorkerRuntime(store)
    runner = DataScriptRunner(store=store, workdir=tmp_path / ".athena" / "runs")
    services = ResearchServices(
        store=store,
        dataset=DatasetService(workdir=tmp_path / ".athena" / "data"),
        runner=runner,
        search=SearchService(ResearchTree()),
        evaluator=TrustedEvaluator(runner),
    )
    planner = DeterministicSupervisorPlanner(journal, state)
    validator = PlanValidator(journal, state)
    executor = PlanExecutor(
        journal=journal,
        state=state,
        runtime=runtime,
        store=store,
        services=services,
    )
    coordinator = SupervisorCoordinator(
        journal=journal,
        state=state,
        planner=planner,
        validator=validator,
        executor=executor,
    )
    coordinator.bind(execution_id)
    return {
        "journal": journal,
        "state": state,
        "execution_id": execution_id,
        "store": store,
        "runtime": runtime,
        "planner": planner,
        "validator": validator,
        "executor": executor,
        "coordinator": coordinator,
        "services": services,
        "runner": runner,
        "tmp_path": tmp_path,
    }


def _configure(components: _Components) -> None:
    """TASK_CONFIGURE：提交 task 事实与 task_config（真实数据文件供 ingest 服务）。"""
    data_dir = Path(components["tmp_path"]) / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "d.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    # PREPARE_EVAL 冻结的真实 uv eval 工作区：stdlib-only，供 scripts.freeze + 可信 evaluator。
    eval_ws = Path(components["tmp_path"]) / "eval_ws"
    eval_ws.mkdir(exist_ok=True)
    (eval_ws / "eval.py").write_text(EVAL_SCRIPT, encoding="utf-8")
    (eval_ws / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'eval'\n"
        "version = '0.1.0'\n"
        "requires-python = '>=3.11'\n"
        "dependencies = []\n",
        encoding="utf-8",
    )
    components["runtime"].eval_workspace = str(eval_ws)
    components["state"].commit_facts(
        {
            "task_ref": "sha256:task",
            "task_config": {
                "init_payload": {
                    "data_path": str(data_dir / "d.csv"),
                    "target": "label",
                },
                "data_payload": {
                    "data_path": str(data_dir / "d.csv"),
                    "target": "label",
                },
                "ideator_payload": {
                    "data_path": str(data_dir / "d.csv"),
                    "target": "label",
                },
                "code_payload": {
                    "data_path": str(data_dir / "d.csv"),
                    "target": "label",
                },
            },
        }
    )


# ---- 阶段投影 ----


def test_project_phase_from_facts() -> None:
    assert project_phase(ProjectFacts()) == ResearchPhase.IDLE
    facts = ProjectFacts(task_ref="sha256:t")
    assert project_phase(facts) == ResearchPhase.PREPARE
    facts = ProjectFacts(
        task_ref="sha256:t",
        eval_spec_ref="sha256:e",
        dataset_role_review_ref="sha256:d",
        dataset_manifest_ref="sha256:m",
        eda_review_ref="sha256:r",
        baseline_experiment_ref="sha256:b",
    )
    assert project_phase(facts) == ResearchPhase.SEARCH
    facts = ProjectFacts(
        task_ref="sha256:t",
        eval_spec_ref="sha256:e",
        dataset_role_review_ref="sha256:d",
        dataset_manifest_ref="sha256:m",
        eda_review_ref="sha256:r",
        baseline_experiment_ref="sha256:b",
        sota_experiment_ref="sha256:s",
        search_stop_ref="sha256:st",
    )
    assert project_phase(facts) == ResearchPhase.VALIDATE
    facts = ProjectFacts(
        task_ref="sha256:t",
        eval_spec_ref="sha256:e",
        dataset_role_review_ref="sha256:d",
        dataset_manifest_ref="sha256:m",
        eda_review_ref="sha256:r",
        baseline_experiment_ref="sha256:b",
        sota_experiment_ref="sha256:s",
        search_stop_ref="sha256:st",
        final_test_attempt_ref="sha256:f",
        validation_result_ref="sha256:v",
    )
    assert project_phase(facts) == ResearchPhase.COMPLETED


# ---- Journal 持久化与 CAS ----


def test_journal_plan_roundtrip(tmp_path: Path) -> None:
    journal = PlanJournal(tmp_path / "s.db")
    op = SupervisorOperation(
        operation_id="op_1",
        operation_type=OperationType.SPAWN_BATCH,
        idempotency_key="spawn:data",
        inputs={"spawns": [{"key": "d", "agent_type": "data", "payload": {}}]},
    )
    plan = SupervisorPlan(
        plan_id="plan_1",
        execution_id="exec_1",
        sequence=1,
        snapshot_version=0,
        reason_code="PREPARE_ANALYSIS",
        operations=[op],
        created_at=_now(),
    )
    journal.save_plan(plan)
    loaded = journal.load_plan("plan_1")
    assert loaded is not None
    assert loaded.reason_code == "PREPARE_ANALYSIS"
    assert loaded.operations[0].operation_type == OperationType.SPAWN_BATCH
    spawns = loaded.operations[0].inputs["spawns"]
    assert isinstance(spawns, list)
    assert spawns[0]["agent_type"] == "data"
    unfinished = journal.load_unfinished_plan("exec_1")
    assert unfinished is not None and unfinished.plan_id == "plan_1"
    journal.close()


def test_journal_fact_cas(tmp_path: Path) -> None:
    journal = PlanJournal(tmp_path / "s.db")
    assert journal.snapshot_version() == 0
    v1 = journal.commit_facts({"task_ref": "sha256:t"}, 0)
    assert v1 == 1
    assert journal.get_fact("task_ref") == "sha256:t"
    with pytest.raises(VersionConflict):
        journal.commit_facts({"eval_ref": "sha256:e"}, 0)  # 旧版本预期 → 拒绝
    v2 = journal.commit_facts({"eval_ref": "sha256:e"}, 1)
    assert v2 == 2
    journal.close()


def test_journal_human_request_roundtrip(tmp_path: Path) -> None:
    from athena.research.supervisor.models import HumanRequest

    journal = PlanJournal(tmp_path / "s.db")
    journal.create_execution(interaction_mode="interactive")
    request = HumanRequest(
        request_id="req_1",
        execution_id="exec_1",
        reason_code="NEEDS_CONFIRM",
        question="确认主指标？",
        options=["accuracy", "f1"],
        created_at=_now(),
    )
    journal.save_human_request(request)
    stored = journal.human_request("req_1")
    assert stored is not None and stored.question == "确认主指标？"
    journal.answer_human_request("req_1", "accuracy", "human")
    answered = journal.human_request("req_1")
    assert answered is not None and answered.answer == "accuracy"
    journal.close()


# ---- Planner 状态机 ----


def test_planner_advances_through_phases(tmp_path: Path) -> None:
    c = _components(tmp_path)
    state, execution_id = c["state"], c["execution_id"]
    planner = c["planner"]

    assert (
        planner.next_plan(
            execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
        )
        is None
    )
    _configure(c)
    facts = state.facts()
    plan = planner.next_plan(execution_id, facts, state.budget(), ControlStatus.RUNNING)
    assert plan is not None and plan.reason_code == "PREPARE_INGEST"
    # ingest = RUN_SERVICE(dataset.ingest) + SPAWN data agent
    assert any(op.operation_type == OperationType.RUN_SERVICE for op in plan.operations)
    assert any(op.operation_type == OperationType.SPAWN_BATCH for op in plan.operations)

    # 补齐 ingest（proposal + manifest）→ PREPARE_ROLE_REVIEW
    state.commit_facts(
        {"dataset_role_proposal_ref": "sha256:p", "dataset_manifest_ref": "sha256:m"}
    )
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_ROLE_REVIEW"

    # 补齐 role_review → PREPARE_EVAL
    state.commit_facts({"dataset_role_review_ref": "sha256:r"})
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_EVAL"

    # 补齐 eval_spec → PREPARE_EDA
    state.commit_facts({"eval_spec_ref": "sha256:e"})
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_EDA"

    # 补齐 eda_report → PREPARE_EDA_REVIEW
    state.commit_facts({"eda_report_ref": "sha256:d"})
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_EDA_REVIEW"

    # 补齐 eda_review → PREPARE_BASELINE
    state.commit_facts({"eda_review_ref": "sha256:rv"})
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_BASELINE"

    # 补齐 baseline → SEARCH_ROUND（真实编排：spawn → register → freeze）
    state.commit_facts({"baseline_experiment_ref": "sha256:b"})
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "SEARCH_ROUND"
    services = [
        op for op in plan.operations if op.operation_type == OperationType.RUN_SERVICE
    ]
    assert [op.inputs["service"] for op in services] == [
        "search.register_hypotheses",
        "evaluation.candidate_batch",
        "search.freeze_ranking_round",
    ]  # 生成真实 search/evaluation operation，而非把 fake ref 直接当 SOTA
    assert any(op.operation_type == OperationType.SPAWN_BATCH for op in plan.operations)
    assert any(op.operation_type == OperationType.WAIT_AGENTS for op in plan.operations)

    # 搜索预算耗尽 → SEARCH_STOP
    budget = state.budget()
    budget.search_experiments_used = budget.max_search_experiments
    state.save_budget(budget)
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "SEARCH_STOP"

    # 已停止但无 final-test → VALIDATE_FINAL_TEST
    state.commit_facts(
        {"search_stop_ref": "sha256:st", "sota_experiment_ref": "sha256:s"}
    )
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "VALIDATE_FINAL_TEST"

    # 全部补齐 → None
    state.commit_facts(
        {"final_test_attempt_ref": "sha256:f", "validation_result_ref": "sha256:v"}
    )
    assert (
        planner.next_plan(
            execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
        )
        is None
    )


def test_search_stopped_without_sota_fails_not_loops(tmp_path: Path) -> None:
    """搜索已停止但无 SOTA → 不再生成 SEARCH_ROUND，直接 FAILED 计划（阶段推进规则）。"""
    c = _components(tmp_path)
    _configure(c)
    state, planner, execution_id = c["state"], c["planner"], c["execution_id"]
    state.commit_facts(
        {
            "dataset_role_proposal_ref": "sha256:p",
            "dataset_manifest_ref": "sha256:m",
            "dataset_role_review_ref": "sha256:r",
            "eval_spec_ref": "sha256:e",
            "eda_report_ref": "sha256:d",
            "eda_review_ref": "sha256:rv",
            "baseline_experiment_ref": "sha256:b",
            "search_stop_ref": "search_stop:accepted",  # 已停止但 sota_experiment_ref 缺失
        }
    )
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "SEARCH_NO_SOTA"
    assert not any(
        op.operation_type is OperationType.SPAWN_BATCH for op in plan.operations
    )  # 不派发新 SEARCH round
    assert any(
        op.operation_type is OperationType.SET_EXECUTION_STATUS
        and op.inputs["status"] == ControlStatus.FAILED.value
        for op in plan.operations
    )
    # 同一状态再次查询仍返回 FAILED 计划（幂等，不无限推进）
    plan2 = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan2 is not None and plan2.reason_code == "SEARCH_NO_SOTA"


def test_search_stops_after_first_ranked_sota(tmp_path: Path) -> None:
    """首版只有一组确定性候选；已有 SOTA 后停止，不能重复创建空搜索轮。"""
    c = _components(tmp_path)
    _configure(c)
    state, planner, execution_id = c["state"], c["planner"], c["execution_id"]
    state.commit_facts(
        {
            "dataset_role_proposal_ref": "sha256:p",
            "dataset_manifest_ref": "sha256:m",
            "dataset_role_review_ref": "sha256:r",
            "eval_spec_ref": "sha256:e",
            "eda_report_ref": "sha256:d",
            "eda_review_ref": "sha256:rv",
            "baseline_experiment_ref": "sha256:b",
            "sota_experiment_ref": "cand_sota",
        }
    )

    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )

    assert plan is not None and plan.reason_code == "SEARCH_STOP"


@pytest.mark.asyncio
async def test_prepare_ingest_data_agent_receives_managed_root(tmp_path: Path) -> None:
    """PREPARE_INGEST 的 DataAgent 收到受管 raw snapshot 绝对路径（非原始源目录）。"""
    c = _components(tmp_path)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    _configure(c)
    plan = c["planner"].next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_INGEST"
    lease = journal.claim_lease(execution_id, "test-owner")
    await c["executor"].execute(plan, lease=lease)
    assert plan.status == PlanStatus.COMPLETED
    data_spawns = [s for s in c["runtime"].spawned if s["agent_type"] == "data"]
    assert data_spawns, "no data worker spawned"
    payload = json.loads(str(data_spawns[-1]["task"]["content"]))
    managed = str(Path(c["tmp_path"]) / ".athena" / "data" / "raw")
    assert payload["kind"] == "role"
    assert payload["data_path"] == managed  # 受管 raw snapshot，非原始源
    assert Path(managed).is_dir() and (Path(managed) / "d.csv").is_file()
    journal.close()


@pytest.mark.asyncio
async def test_prepare_eval_uses_target_from_role_proposal(tmp_path: Path) -> None:
    """未显式配置 target 时，InitAgent 使用已评审 proposal 的 target_column。"""
    c = _components(tmp_path)
    _configure(c)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    task_config = dict(journal.get_fact("task_config"))
    for payload in task_config.values():
        payload["target"] = ""
    state.commit_facts({"task_config": task_config})
    c["runtime"].next_payload = {
        "role_proposal": "d.csv is training data",
        "data_files": ["d.csv"],
        "target_column": "label",
        "reasoning": "label is the supervised target",
    }
    lease = journal.claim_lease(execution_id, "test-owner")

    for _ in range(4):
        plan = c["planner"].next_plan(
            execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
        )
        assert plan is not None
        await c["executor"].execute(plan, lease=lease)

    init_spawn = next(
        spawn for spawn in c["runtime"].spawned if spawn["agent_type"] == "init"
    )
    payload = json.loads(str(init_spawn["task"]["content"]))
    assert payload["target"] == "label"
    journal.close()


@pytest.mark.asyncio
async def test_all_data_reading_workers_receive_managed_root(tmp_path: Path) -> None:
    """全链路：所有带 data_path 的 worker（init/EDA data/SEARCH ideator+code/final code）都指向受管 raw。"""
    c = _components(tmp_path)
    _configure(c)
    budget = c["state"].budget()
    budget.max_search_experiments = 1
    c["state"].save_budget(budget)
    terminal = await c["coordinator"].run()
    assert terminal == ControlStatus.RUNNING
    managed = str(Path(c["tmp_path"]) / ".athena" / "data" / "raw")
    assert c["state"].facts().validation_result_ref is not None  # 链闭合
    data_readers = {
        "init",
        "data",
        "ideator",
        "code",
    }
    data_kinds = []
    for spawn in c["runtime"].spawned:
        if spawn["agent_type"] not in data_readers:
            continue
        payload = json.loads(str(spawn["task"]["content"]))
        if spawn["agent_type"] == "data":
            data_kinds.append(payload.get("kind"))
        if "data_path" in payload:
            assert payload["data_path"] == managed, spawn
    assert data_kinds == ["role", "eda"]
    c["journal"].close()


def test_planner_respects_control_status(tmp_path: Path) -> None:
    c = _components(tmp_path)
    _configure(c)
    assert (
        c["planner"].next_plan(
            c["execution_id"],
            c["state"].facts(),
            c["state"].budget(),
            ControlStatus.PAUSED,
        )
        is None
    )


# ---- Validator ----


def test_validator_rejects_stale_snapshot_and_unallowed_worker(tmp_path: Path) -> None:
    c = _components(tmp_path)
    _configure(c)
    bad_op = SupervisorOperation(
        operation_id="op_x",
        operation_type=OperationType.SPAWN_BATCH,
        idempotency_key="spawn:hack",
        inputs={"spawns": [{"key": "h", "agent_type": "sudo", "payload": {}}]},
    )
    plan = SupervisorPlan(
        plan_id="plan_x",
        execution_id=c["execution_id"],
        sequence=1,
        snapshot_version=999,  # 过期版本
        reason_code="PREPARE_EVAL",
        operations=[bad_op],
        created_at=_now(),
    )
    with pytest.raises(ValidationError) as excinfo:
        c["validator"].validate(plan, c["state"].budget(), ControlStatus.RUNNING)
    message = str(excinfo.value)
    assert "stale snapshot_version" in message
    assert "worker type not allowed: sudo" in message


def test_validator_rejects_exhausted_budget_and_duplicate_key(tmp_path: Path) -> None:
    c = _components(tmp_path)
    _configure(c)
    budget = c["state"].budget()
    budget.plans_used = budget.max_total_plans
    op = SupervisorOperation(
        operation_id="op_1",
        operation_type=OperationType.SPAWN_BATCH,
        idempotency_key="spawn:data",
        inputs={"spawns": [{"key": "d", "agent_type": "data", "payload": {}}]},
    )
    plan = SupervisorPlan(
        plan_id="plan_x",
        execution_id=c["execution_id"],
        sequence=1,
        snapshot_version=c["journal"].snapshot_version(),
        reason_code="PREPARE_ANALYSIS",
        operations=[op, op.model_copy()],  # 同一 idempotency_key 重复
        created_at=_now(),
    )
    with pytest.raises(ValidationError) as excinfo:
        c["validator"].validate(plan, budget, ControlStatus.RUNNING)
    message = str(excinfo.value)
    assert "plan budget exhausted" in message
    assert "duplicate idempotency_key" in message


# ---- Executor：spawn → wait → commit 闭环 ----


@pytest.mark.asyncio
async def test_executor_spawn_wait_commit(tmp_path: Path) -> None:
    c = _components(tmp_path)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    executor = c["executor"]

    spawn = SupervisorOperation(
        operation_id="op_spawn",
        operation_type=OperationType.SPAWN_BATCH,
        idempotency_key="spawn:data",
        inputs={"spawns": [{"key": "d", "agent_type": "data", "payload": {}}]},
    )
    wait = SupervisorOperation(
        operation_id="op_wait",
        operation_type=OperationType.WAIT_AGENTS,
        idempotency_key="wait:data",
        inputs={"from_op": "op_spawn", "keys": ["d"]},
    )
    commit = SupervisorOperation(
        operation_id="op_commit",
        operation_type=OperationType.COMMIT_FACTS,
        idempotency_key="commit:role_review",
        inputs={
            "facts": {"dataset_role_review_ref": {"from_op": "op_wait", "worker": "d"}},
            "reason": "PREPARE_ANALYSIS",
        },
    )
    plan = SupervisorPlan(
        plan_id="plan_e",
        execution_id=execution_id,
        sequence=1,
        snapshot_version=journal.snapshot_version(),
        reason_code="PREPARE_ANALYSIS",
        operations=[spawn, wait, commit],
        created_at=_now(),
    )
    lease = journal.claim_lease(execution_id, "test-owner")
    await executor.execute(plan, lease=lease)
    assert plan.status == PlanStatus.COMPLETED
    assert all(op.status == OperationStatus.SUCCEEDED for op in plan.operations)
    role_review_ref = state.facts().dataset_role_review_ref
    assert role_review_ref is not None and role_review_ref.startswith("sha256:")
    assert c["runtime"].spawned[0]["agent_type"] == "data"
    # 恢复的 Plan 应保留 operation outputs
    loaded = journal.load_plan("plan_e")
    assert loaded is not None and loaded.operations
    committed = loaded.operations[-1].outputs.get("committed")
    assert isinstance(committed, dict)
    assert committed["dataset_role_review_ref"] == role_review_ref


@pytest.mark.asyncio
async def test_run_service_resolves_request_from_worker_result(tmp_path: Path) -> None:
    """RUN_SERVICE request 字段可从 worker 结果解析（LLM 产物动态喂给确定性服务）。"""
    c = _components(tmp_path)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    executor, runtime = c["executor"], c["runtime"]
    runtime.next_payload = {
        "candidates": [
            {"candidate_id": "a", "test_score": 0.9, "direction": "maximize"},
            {"candidate_id": "b", "test_score": 0.7, "direction": "maximize"},
        ]
    }
    spawn = SupervisorOperation(
        operation_id="op_spawn",
        operation_type=OperationType.SPAWN_BATCH,
        idempotency_key="spawn:data",
        inputs={"spawns": [{"key": "data", "agent_type": "data", "payload": {}}]},
    )
    wait = SupervisorOperation(
        operation_id="op_wait",
        operation_type=OperationType.WAIT_AGENTS,
        idempotency_key="wait:data",
        inputs={"from_op": "op_spawn", "keys": ["data"]},
    )
    service = SupervisorOperation(
        operation_id="op_svc",
        operation_type=OperationType.RUN_SERVICE,
        idempotency_key="run:freeze_round",
        inputs={
            "service": "search.freeze_ranking_round",
            "request": {
                "parent_score": 0.8,
                "selected_count": 1,
                "candidates": {
                    "from_op": "op_wait",
                    "worker": "data",
                    "field": "candidates",
                },
            },
        },
    )
    plan = SupervisorPlan(
        plan_id="plan_svc",
        execution_id=execution_id,
        sequence=1,
        snapshot_version=journal.snapshot_version(),
        reason_code="SEARCH_ROUND",
        operations=[spawn, wait, service],
        created_at=_now(),
    )
    lease = journal.claim_lease(execution_id, "test-owner")
    await executor.execute(plan, lease=lease)
    assert plan.status == PlanStatus.COMPLETED
    assert state.facts().ranking_round_ref is not None
    assert (
        state.facts().sota_experiment_ref == "a"
    )  # 从 worker 解析的候选按 test_score 定 SOTA
    journal.close()


@pytest.mark.asyncio
async def test_search_round_service_consumes_budget_atomically(tmp_path: Path) -> None:
    """SEARCH_ROUND 的 fact-producing RUN_SERVICE 同事务消费搜索预算；幂等重放不重复。"""
    c = _components(tmp_path)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    executor, runtime = c["executor"], c["runtime"]
    runtime.next_payload = {
        "candidates": [
            {"candidate_id": "a", "test_score": 0.9, "direction": "maximize"}
        ]
    }
    spawn = SupervisorOperation(
        operation_id="op_spawn",
        operation_type=OperationType.SPAWN_BATCH,
        idempotency_key="spawn:data",
        inputs={"spawns": [{"key": "data", "agent_type": "data", "payload": {}}]},
    )
    wait = SupervisorOperation(
        operation_id="op_wait",
        operation_type=OperationType.WAIT_AGENTS,
        idempotency_key="wait:data",
        inputs={"from_op": "op_spawn", "keys": ["data"]},
    )
    service = SupervisorOperation(
        operation_id="op_svc",
        operation_type=OperationType.RUN_SERVICE,
        idempotency_key="run:freeze_round",
        inputs={
            "service": "search.freeze_ranking_round",
            "request": {
                "parent_score": 0.8,
                "selected_count": 1,
                "candidates": {
                    "from_op": "op_wait",
                    "worker": "data",
                    "field": "candidates",
                },
            },
        },
    )
    plan = SupervisorPlan(
        plan_id="plan_budget",
        execution_id=execution_id,
        sequence=1,
        snapshot_version=journal.snapshot_version(),
        reason_code="SEARCH_ROUND",
        operations=[spawn, wait, service],
        created_at=_now(),
    )
    lease = journal.claim_lease(execution_id, "test-owner")
    await executor.execute(plan, lease=lease)
    assert plan.status == PlanStatus.COMPLETED
    assert state.budget().search_experiments_used == 1  # 一轮一次，同事务消费
    # 幂等重放（崩溃恢复）不再重复消费预算
    await executor.execute(plan, lease=lease)
    assert state.budget().search_experiments_used == 1
    journal.close()


@pytest.mark.asyncio
async def test_real_search_round_executes_and_commits_sota(tmp_path: Path) -> None:
    """Planner 的真实 SEARCH_ROUND 计划经 executor 跑通：register+eval+freeze 服务执行，SOTA 由可信 evaluator 判定。"""
    c = _components(tmp_path)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    _configure(c)
    # 补齐 PREPARE 全部 gate facts → SEARCH 阶段；eval_bundle_ref 为真实冻结 bundle。
    state.commit_facts(
        {
            "dataset_role_proposal_ref": "sha256:p",
            "dataset_manifest_ref": "sha256:m",
            "dataset_role_review_ref": "sha256:r",
            "eval_spec_ref": "sha256:e",
            "eda_report_ref": "sha256:d",
            "eda_review_ref": "sha256:rv",
            "baseline_experiment_ref": "sha256:b",
            "dataset_managed_root": str(Path(c["tmp_path"]) / "data" / "raw"),
        }
    )
    eval_ws = Path(c["tmp_path"]) / "eval_ws"
    bundle = await c["runner"].freeze(eval_ws, BundleMetadata(entrypoint="eval.py"))
    state.commit_facts(
        {"eval_bundle_ref": await c["store"].put_text(bundle.model_dump_json())}
    )
    plan = c["planner"].next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "SEARCH_ROUND"
    services = [
        op for op in plan.operations if op.operation_type == OperationType.RUN_SERVICE
    ]
    assert [op.inputs["service"] for op in services] == [
        "search.register_hypotheses",
        "evaluation.candidate_batch",
        "search.freeze_ranking_round",
    ]
    lease = journal.claim_lease(execution_id, "test-owner")
    await c["executor"].execute(plan, lease=lease)
    assert plan.status == PlanStatus.COMPLETED
    facts = state.facts()
    assert facts.graph_ref is not None  # register 服务提交 graph_ref
    assert facts.ranking_round_ref is not None  # freeze 服务提交 RankingRound
    assert (
        facts.sota_experiment_ref is not None
    )  # SOTA 来自 search service（非 fake ref）
    # 候选分数来自可信 evaluator（运行冻结 eval bundle），不是 CodeAgent fallback 的 test_score
    assert facts.sota_experiment_ref.startswith("cand_")
    ranking = json.loads(await c["store"].get_text(facts.ranking_round_ref))
    # fallback 硬编码 test_score=0.85；可信 evaluator 对 [0,1] vs [0,1] 算 accuracy=1.0
    assert list(ranking["scores"].values()) == [pytest.approx(1.0)]
    assert state.budget().search_experiments_used == 1  # freeze 事务原子消费一次
    journal.close()


@pytest.mark.asyncio
async def test_validate_plan_runs_final_test_state_machine(tmp_path: Path) -> None:
    """VALIDATE 真实编排：reserve → RUNNING → PREDICTIONS_WRITTEN → SCORED → COMMITTED，提交结果。"""
    c = _components(tmp_path)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    _configure(c)
    # 补齐 PREPARE + SEARCH facts → VALIDATE 阶段；eval_bundle_ref 为真实冻结 bundle。
    state.commit_facts(
        {
            "dataset_role_proposal_ref": "sha256:p",
            "dataset_manifest_ref": "sha256:m",
            "dataset_role_review_ref": "sha256:r",
            "eval_spec_ref": "sha256:e",
            "eda_report_ref": "sha256:d",
            "eda_review_ref": "sha256:rv",
            "baseline_experiment_ref": "sha256:b",
            "sota_experiment_ref": "cand_sota",
            "search_stop_ref": "sha256:st",
            "dataset_managed_root": str(Path(c["tmp_path"]) / "data" / "raw"),
        }
    )
    eval_ws = Path(c["tmp_path"]) / "eval_ws"
    bundle = await c["runner"].freeze(eval_ws, BundleMetadata(entrypoint="eval.py"))
    state.commit_facts(
        {"eval_bundle_ref": await c["store"].put_text(bundle.model_dump_json())}
    )
    plan = c["planner"].next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "VALIDATE_FINAL_TEST"
    assert any(
        op.operation_type == OperationType.RESERVE_FINAL_TEST for op in plan.operations
    )
    advances = [
        op
        for op in plan.operations
        if op.operation_type == OperationType.ADVANCE_FINAL_TEST
    ]
    assert [op.inputs["expected_status"] for op in advances] == [
        "RUNNING",
        "PREDICTIONS_WRITTEN",
        "SCORED",
        "COMMITTED",
    ]
    lease = journal.claim_lease(execution_id, "test-owner")
    await c["executor"].execute(plan, lease=lease)
    assert plan.status == PlanStatus.COMPLETED
    facts = state.facts()
    assert facts.final_test_attempt_ref is not None  # 唯一 attempt 提交
    assert facts.validation_result_ref is not None  # gap/warning 结果提交
    # attempt 状态机到 COMMITTED，score 持久化
    attempt = journal._attempt_from_row(
        journal._conn.execute(
            "SELECT * FROM final_test_attempts WHERE attempt_id = ?",
            (facts.final_test_attempt_ref,),
        ).fetchone()
    )
    assert attempt.status == "COMMITTED"
    # final-test score 来自可信 evaluator（运行冻结 eval bundle 的 accuracy=1.0），
    # 不是 CodeAgent fallback 的 test_score=0.85；并有独立 score Artifact。
    assert attempt.final_test_score == pytest.approx(1.0)
    assert attempt.score_ref is not None and attempt.score_ref.startswith("sha256:")
    journal.close()


@pytest.mark.asyncio
async def test_executor_fails_fast_on_worker_failure(tmp_path: Path) -> None:
    c = _components(tmp_path)
    journal, execution_id = c["journal"], c["execution_id"]
    executor = c["executor"]
    # 注入失败的 wait：FakeRunResult 无结果 → _extract_result_ref 抛业务失败
    spawn = SupervisorOperation(
        operation_id="op_s",
        operation_type=OperationType.SPAWN_BATCH,
        idempotency_key="spawn:code",
        inputs={"spawns": [{"key": "c", "agent_type": "code", "payload": {}}]},
    )
    wait = SupervisorOperation(
        operation_id="op_w",
        operation_type=OperationType.WAIT_AGENTS,
        idempotency_key="wait:code",
        inputs={"from_op": "op_s", "keys": ["c"]},
    )
    # 让下一个 spawn 的 worker 失败 → WAIT 业务失败
    c["runtime"].fail_next = True
    plan = SupervisorPlan(
        plan_id="plan_f",
        execution_id=execution_id,
        sequence=1,
        snapshot_version=journal.snapshot_version(),
        reason_code="SEARCH_ROUND",
        operations=[spawn, wait],
        created_at=_now(),
    )
    lease = journal.claim_lease(execution_id, "test-owner")
    await executor.execute(plan, lease=lease)
    assert plan.status == PlanStatus.FAILED
    assert plan.operations[1].status == OperationStatus.FAILED
    error = plan.operations[1].error
    assert error is not None and "worker run not completed" in error


# ---- Coordinator 全流程 ----


@pytest.mark.asyncio
async def test_coordinator_stops_on_failed_plan(tmp_path: Path) -> None:
    """业务失败（如 dataset.ingest 缺源）→ 不重发，execution 进入终态 FAILED（STATUS 不永久轮询）。"""
    c = _components(tmp_path)
    c["state"].commit_facts(
        {
            "task_ref": "sha256:task",
            "task_config": {
                "init_payload": {
                    "data_path": str(tmp_path / "nope.csv"),
                    "target": "label",
                },
                "data_payload": {
                    "data_path": str(tmp_path / "nope.csv"),
                    "target": "label",
                },
            },
        }
    )
    events: list[str] = []
    c["coordinator"].bind(
        c["execution_id"], on_event=lambda kind, data: events.append(kind)
    )
    terminal = await c["coordinator"].run()
    assert terminal == ControlStatus.FAILED  # 业务失败投影为终态 FAILED，不重试
    assert c["state"].control_status() is ControlStatus.FAILED
    assert "plan/completed" in events  # ingest 计划执行失败（plan 状态 FAILED）
    assert c["journal"].load_unfinished_plan(c["execution_id"]) is None
    # 没有第二个失败计划被创建
    failed_plans = sum(1 for _kind in events if _kind == "plan/completed")
    assert failed_plans == 1
    c["journal"].close()


@pytest.mark.asyncio
async def test_coordinator_worker_failure_projects_failed(tmp_path: Path) -> None:
    """worker 失败（如真实 wait:data）→ Plan FAILED → execution 终态 FAILED（STATUS 不永久轮询）。"""
    c = _components(tmp_path)
    _configure(c)
    c["runtime"].fail_next = True  # 下一个 spawn 的 worker 失败 → WAIT 业务失败
    terminal = await c["coordinator"].run()
    assert terminal == ControlStatus.FAILED
    assert c["state"].control_status() is ControlStatus.FAILED
    assert c["journal"].load_unfinished_plan(c["execution_id"]) is None
    c["journal"].close()


@pytest.mark.asyncio
async def test_coordinator_drives_to_completed(tmp_path: Path) -> None:
    c = _components(tmp_path)
    _configure(c)
    # 预算收紧：一次搜索即耗尽 → 一轮后停止
    budget = c["state"].budget()
    budget.max_search_experiments = 1
    c["state"].save_budget(budget)

    events: list[tuple[str, dict]] = []
    c["coordinator"].bind(
        c["execution_id"],
        on_event=lambda kind, data: events.append((kind, data)),
    )
    terminal = await c["coordinator"].run()
    assert terminal == ControlStatus.RUNNING
    assert c["state"].facts().validation_result_ref is not None
    assert c["state"].facts().search_stop_ref is not None
    assert project_phase(c["state"].facts()) == ResearchPhase.COMPLETED
    assert any(kind == "plan/completed" for kind, _ in events)
    # 全部 Plan 均完成，无残留未完成 Plan
    assert c["journal"].load_unfinished_plan(c["execution_id"]) is None
    c["journal"].close()
