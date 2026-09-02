"""候选必须知道"训练用哪份数据"。

真机（2026-08-29，qwen3.7-plus 跑 TESS 恒星耀发任务）：平台按 TIC 分组切好了
train/search/final，却只把这件事告诉了 evaluator。基线 agent 拿到的任务文本里唯一
的指路牌是 ``Dataset path: <原始 csv>``，于是它去那个目录里自己找划分，用了旁边
一套早先切的 ``windows_{train,val,test}.csv``。

平台 search split 的 14703 行里，**11978 行（81.5%）落在它的训练集里**，PR-AUC
因此报到 0.9736——参考值是 0.891。评估器完全正常：它只比对 predictions 与 labels，
结构上无法知道模型是在哪些行上拟合的。

候选看不到任务文本（``PlanInput`` 只经 ``context_refs``，那是死信道），所以这条
约束必须持久化在 ``state.data_contract`` 上，再由 Supervisor 每一轮拼进 content。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.research.prepare.data import DataContract
from athena.research.supervisor.deps import (
    PhaseActions,
    ResearchActions,
    SearchServices,
    SupervisorDeps,
    SupervisorPaths,
    SupervisorRuntime,
)
from athena.research.supervisor.prompt_context import data_contract_block
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor


def test_the_block_names_the_training_file_and_the_reason() -> None:
    block = data_contract_block(
        "Train ONLY on /p/data_split/train.csv.\nDo NOT read /p/model_input.csv"
    )

    assert "/p/data_split/train.csv" in block
    assert "Do NOT read /p/model_input.csv" in block
    assert "invalidates your score" in block


def test_an_absent_contract_adds_nothing() -> None:
    assert data_contract_block("") == ""
    assert data_contract_block("   \n ") == ""


def test_data_contract_value_object_prompts_train_only_never_raw_and_predict_env(
    tmp_path,
) -> None:
    """The value object must render every platform-data rule for candidates.

    The durable contract and the baseline task prompt both have to say the same
    three facts: train only on the platform train file, never read the raw
    dataset, and read ATHENA_PREDICT_FEATURES for the prediction rows.
    """
    raw = tmp_path / "model_input.csv"
    train = tmp_path / "data_split" / "train.csv"
    search = tmp_path / "data_split" / "search_features.csv"
    contract = DataContract(
        train_csv=train,
        predict_features_csv=search,
        dataset_path=raw,
        group_column="TIC",
    )

    contract_text = contract.contract_text()
    assert "Train ONLY on" in contract_text
    assert str(train) in contract_text
    assert "Do NOT read" in contract_text
    assert str(raw) in contract_text
    assert "ATHENA_PREDICT_FEATURES" in contract_text

    candidate_text = contract.candidate_task("baseline task")
    assert "Train ONLY on" in candidate_text
    assert str(train) in candidate_text
    assert "Do NOT read" in candidate_text
    assert str(raw) in candidate_text
    assert "ATHENA_PREDICT_FEATURES" in candidate_text


def test_the_state_carries_the_contract_across_a_save_load(tmp_path) -> None:
    """SEARCH 可能在 PREPARE 之后很久才跑，甚至跨进程续跑。"""
    path = tmp_path / "state.json"
    state = ResearchState(
        status="RUNNING", phase="SEARCH", search_limit=10, concurrency=1
    )
    state.data_contract = "Train ONLY on /p/data_split/train.csv."
    state.save(path)

    assert ResearchState.load(path).data_contract == (
        "Train ONLY on /p/data_split/train.csv."
    )


@pytest.mark.asyncio
async def test_every_search_turn_carries_the_data_contract() -> None:
    """光有渲染函数不够——真正断掉的是"它接上了没有"。"""
    sent: dict[str, object] = {}

    class Agents:
        async def followup(self, plan_id, message):
            sent["content"] = message["content"]
            return "run-1"

        async def wait_run(self, run_id):
            return SimpleNamespace(status="completed", response_ref=None)

    def plan_state():
        state = SimpleNamespace(
            turns_used=0,
            turn_limit=4,
            patience=2,
            stale_rounds=0,
            context_ref="sha256:ctx",
            best_ref=None,
            last_failure=None,
        )
        state.model_copy = lambda update: state
        return state

    tree = SimpleNamespace(
        get_hypothesis=lambda plan_id: SimpleNamespace(
            statement="s", intervention="i", expected_effect="e"
        )
    )
    state = SimpleNamespace(
        plans={"hyp_abc": plan_state()},
        data_contract="Train ONLY on /p/data_split/train.csv.",
    )
    supervisor = _supervisor(tree, state, Agents())

    await supervisor._plans.run_turn("hyp_abc")

    assert "/p/data_split/train.csv" in sent["content"]


@pytest.mark.asyncio
async def test_no_contract_means_no_block_in_the_turn() -> None:
    """平台不拥有划分时（目录数据、Kaggle 竞赛），不该凭空多出一段约束。"""
    sent: dict[str, object] = {}

    class Agents:
        async def followup(self, plan_id, message):
            sent["content"] = message["content"]
            return "run-1"

        async def wait_run(self, run_id):
            return SimpleNamespace(status="completed", response_ref=None)

    state = SimpleNamespace(
        turns_used=0,
        turn_limit=4,
        patience=2,
        stale_rounds=0,
        context_ref="sha256:ctx",
        best_ref=None,
        last_failure=None,
    )
    state.model_copy = lambda update: state

    tree = SimpleNamespace(
        get_hypothesis=lambda plan_id: SimpleNamespace(
            statement="s", intervention="i", expected_effect="e"
        )
    )
    durable = SimpleNamespace(plans={"hyp_abc": state}, data_contract=None)
    supervisor = _supervisor(tree, durable, Agents())

    await supervisor._plans.run_turn("hyp_abc")

    assert "Data contract" not in sent["content"]


async def _noop() -> None:
    """占位的持久化。"""


async def _handoff(plan_id: str) -> str:
    """占位的评估契约。"""
    return "id column: row_id"


def _supervisor(tree, state, agents) -> Supervisor:
    async def publish(_kind, _payload):
        return None

    supervisor = Supervisor(
        state=state,
        tree=tree,
        deps=SupervisorDeps(
            paths=SupervisorPaths(Path("."), Path("state.json"), Path("tree.json")),
            runtime=SupervisorRuntime(SimpleNamespace(), agents, SimpleNamespace()),
            research=ResearchActions(_unused_plan, _unused_supervisor),
            phases=PhaseActions(publish),
            search=SearchServices(Scheduler(), Recovery()),
        ),
    )
    supervisor._plans.persist_state = _noop

    async def plan_input(plan_id: str):
        return SimpleNamespace(
            task_context="",
            hypothesis=tree.get_hypothesis(plan_id),
            eval_handoff=await _handoff(plan_id),
        )

    supervisor._plans.plan_input = plan_input
    return supervisor


async def _unused_plan(_plan_id, _state):
    raise AssertionError("plan turn must not run")


async def _unused_supervisor(_text):
    raise AssertionError("supervisor turn must not run")
