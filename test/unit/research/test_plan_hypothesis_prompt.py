"""SEARCH 的 PlanAgent 到底看不看得见它要实现的那条假设。

假设此前只经 ``PlanInput`` 走 ``context_refs``，而那是一条**死信道**：``base_runner``
只把 trigger 的 ``content`` 当作 model 的 user prompt，``context_refs`` 仅以 sha256
引用的形式出现在信封里，通用工具集里也没有按 ref 取正文的算子。

真机（2026-08-18）：一条臂 6 次实验无一实现分配给它的假设——3 次直接 ``abandon``，
理由逐字是 "The user message does not contain explicit hypothesis text"；另 3 次自行
编了干预，其中两条不同的假设产出**逐字节相同的预测**。

这些用例钉的是"假设必须出现在 content 里"，因为那是唯一到得了 model 的通道。
"""

import unittest
from types import SimpleNamespace

from athena.research.supervisor.experiment import hypothesis_block
from athena.research.supervisor.supervisor import Supervisor


class HypothesisBlockTest(unittest.TestCase):
    def test_all_three_fields_reach_the_text(self) -> None:
        block = hypothesis_block(
            "Interaction features help",
            "Add debt_ratio * revolving_utilization",
            "average precision rises by 0.01",
        )

        self.assertIn("Interaction features help", block)
        self.assertIn("Add debt_ratio * revolving_utilization", block)
        self.assertIn("average precision rises by 0.01", block)

    def test_it_tells_the_agent_not_to_improvise(self) -> None:
        """真机上 agent 编了自己的干预，所以这句话必须在。"""
        block = hypothesis_block("claim", "do the thing", "metric rises")

        self.assertIn("implement exactly this", block)

    def test_an_empty_hypothesis_adds_nothing(self) -> None:
        self.assertEqual("", hypothesis_block("", "", ""))
        self.assertEqual("", hypothesis_block("  ", "\n", " \t "))

    def test_missing_fields_are_dropped_not_rendered_blank(self) -> None:
        block = hypothesis_block("claim only", "", "")

        self.assertIn("claim only", block)
        self.assertNotIn("Intervention to implement:", block)


class SupervisorHypothesisBlockTest(unittest.TestCase):
    """``_hypothesis_block`` 从树里取假设；取不到时降级为空串而不是让 Plan 挂掉。"""

    @staticmethod
    def _supervisor(tree) -> Supervisor:
        supervisor = Supervisor.__new__(Supervisor)
        supervisor.tree = tree
        return supervisor

    def test_it_renders_the_hypothesis_bound_to_this_plan(self) -> None:
        hypothesis = SimpleNamespace(
            statement="noise columns hurt",
            intervention="drop noise_00..noise_09",
            expected_effect="AP rises",
        )
        tree = SimpleNamespace(get_hypothesis=lambda plan_id: hypothesis)

        block = self._supervisor(tree)._hypothesis_block("hyp_abc")

        self.assertIn("drop noise_00..noise_09", block)

    def test_an_unknown_plan_degrades_to_empty(self) -> None:
        def missing(plan_id: str):
            raise KeyError(plan_id)

        tree = SimpleNamespace(get_hypothesis=missing)

        self.assertEqual("", self._supervisor(tree)._hypothesis_block("hyp_gone"))


class PlanTurnMessageTest(unittest.IsolatedAsyncioTestCase):
    """真正断掉的是集成点：发给 PlanAgent 的那条 message 里到底有没有假设。

    上面几条只验渲染函数；这一条验它**接上了**。缺了这一条，``hypothesis_block``
    可以写得完美无缺却从来没有人调用它——而那正是修复之前的状态。
    """

    async def test_the_turn_message_carries_the_hypothesis(self) -> None:
        sent: dict[str, object] = {}

        class Agents:
            async def followup(self, plan_id, message):
                sent["plan_id"] = plan_id
                sent["content"] = message["content"]
                return "run-1"

            async def wait_run(self, run_id):
                return SimpleNamespace(result_ref=None)

        supervisor = Supervisor.__new__(Supervisor)
        supervisor.tree = SimpleNamespace(
            get_hypothesis=lambda plan_id: SimpleNamespace(
                statement="dropping noise columns reduces overfitting",
                intervention="exclude noise_00 through noise_09",
                expected_effect="average precision rises",
            )
        )
        supervisor.state = SimpleNamespace(
            plans={"hyp_abc": _plan_state()}, data_contract=None
        )
        supervisor._agents = Agents()
        supervisor._publish_agent_event = None
        supervisor._persist_state = _noop
        supervisor._plan_handoff = _handoff
        supervisor._corpus_block = lambda plan_id: ""

        await supervisor._run_one_turn("hyp_abc")

        content = sent["content"]
        self.assertIn("exclude noise_00 through noise_09", content)
        self.assertIn("dropping noise columns reduces overfitting", content)
        # 契约那一段是同一条教训的第一处落点，不能在修第二处时把它弄丢
        self.assertIn("row_id", content)


class PlanTurnFailureFeedbackTest(unittest.IsolatedAsyncioTestCase):
    """同一条教训的第三处落点：上一轮为什么没跑成，Agent 必须看得见。

    失败结果一直带着 ``kind``/``error``，但它们只进了 evidence artifact 和事件流。
    Agent 下一轮收到的仍然是 ``Continue Plan …; turns used: N``，于是只能把同一份
    manifest 原样再交一次——同一个 ModuleNotFoundError 能烧掉一整条 Plan。
    """

    @staticmethod
    def _supervisor(sent: dict, state) -> Supervisor:
        class Agents:
            async def followup(self, plan_id, message):
                sent["content"] = message["content"]
                return "run-1"

            async def wait_run(self, run_id):
                return SimpleNamespace(result_ref=None)

        supervisor = Supervisor.__new__(Supervisor)
        supervisor.tree = SimpleNamespace(
            get_hypothesis=lambda plan_id: SimpleNamespace(
                statement="s", intervention="i", expected_effect="e"
            )
        )
        supervisor.state = SimpleNamespace(plans={"hyp_abc": state}, data_contract=None)
        supervisor._agents = Agents()
        supervisor._publish_agent_event = None
        supervisor._persist_state = _noop
        supervisor._plan_handoff = _handoff
        supervisor._corpus_block = lambda plan_id: ""
        return supervisor

    async def test_the_previous_failure_reaches_the_next_prompt(self) -> None:
        sent: dict[str, object] = {}
        state = _plan_state(
            last_failure="execution_failed: command failed (exit 1): ModuleNotFoundError: astropy"
        )
        supervisor = self._supervisor(sent, state)

        await supervisor._run_one_turn("hyp_abc")

        content = sent["content"]
        self.assertIn("ModuleNotFoundError: astropy", content)
        self.assertIn("execution_failed", content)
        self.assertIn("Do not resubmit the same experiment unchanged", content)

    async def test_the_failure_is_consumed_once_not_repeated_forever(self) -> None:
        """已经修好的错误不该一直挂在 prompt 里误导后续每一轮。"""
        sent: dict[str, object] = {}
        supervisor = self._supervisor(
            sent, _plan_state(last_failure="output_failed: nope")
        )

        await supervisor._run_one_turn("hyp_abc")

        self.assertIsNone(supervisor.state.plans["hyp_abc"].last_failure)

    async def test_a_clean_previous_turn_adds_nothing(self) -> None:
        sent: dict[str, object] = {}
        supervisor = self._supervisor(sent, _plan_state())

        await supervisor._run_one_turn("hyp_abc")

        self.assertNotIn("Previous attempt failed", sent["content"])

    async def test_a_failed_result_is_recorded_for_the_next_turn(self) -> None:
        supervisor = self._supervisor({}, _plan_state())

        await supervisor._record_turn_failure(
            "hyp_abc",
            SimpleNamespace(kind="scoring_failed", error="primary score is not finite"),
        )

        stored = supervisor.state.plans["hyp_abc"].last_failure
        self.assertIn("scoring_failed", stored)
        self.assertIn("primary score is not finite", stored)

    async def test_a_scored_result_records_nothing(self) -> None:
        supervisor = self._supervisor({}, _plan_state())

        await supervisor._record_turn_failure(
            "hyp_abc", SimpleNamespace(kind="scored", error=None)
        )

        self.assertIsNone(supervisor.state.plans["hyp_abc"].last_failure)


def _plan_state(*, last_failure: str | None = None) -> SimpleNamespace:
    """A stand-in for ``PlanState`` that supports the one ``model_copy`` we use."""
    state = SimpleNamespace(
        turns_used=0,
        turn_limit=4,
        patience=2,
        stale_rounds=0,
        context_ref="sha256:ctx",
        best_ref=None,
        last_failure=last_failure,
    )

    def model_copy(update):
        clone = _plan_state(last_failure=state.last_failure)
        for key, value in update.items():
            setattr(clone, key, value)
        return clone

    state.model_copy = model_copy
    return state


async def _noop() -> None:
    """占位的持久化。"""


async def _handoff(plan_id: str) -> str:
    """占位的评估契约。"""
    return "id column: row_id"


if __name__ == "__main__":
    unittest.main()
