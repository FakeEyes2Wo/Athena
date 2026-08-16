"""Phase execution for the research lifecycle (PREPARE / VALIDATE / plan turn).

拆自 ``ResearchRuntime``：把 phase 编排（run_plan_turn / run_prepare_phase /
run_validation_phase / review_validation_diff）集中到一个组合单元。持有
``runtime`` 引用以访问组合根的共享基础设施；避免在构造期重复注入十余个
可变字段。
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athena.agents.prepare_agent import register_evaluator_agent, register_prepare_agent
from athena.agents.supervisor_agent import MAX_PLAN_TURNS
from athena.agents.validate_agent import register_validate_agent
from athena.core.contracts import ArtifactRef
from athena.execution.runtime import ExecutionContext
from athena.research.contracts import ValidationResult
from athena.research.supervisor.experiment import PlanRunner, PlanTurnResult
from athena.research.supervisor.prepare import (
    PrepareResult,
    run_evaluator_plan,
    run_prepare_plan,
)
from athena.research.supervisor.validation import (
    ValidationDiffReview,
    ValidationInput,
    run_validation_plan,
    validation_key,
)
from athena.utils.single_turn_chat import single_turn_chat

if TYPE_CHECKING:
    from athena.research.runtime import ResearchRuntime


class PhaseRunner:
    """Run PREPARE / VALIDATE / plan phases via the runtime's shared infra."""

    def __init__(self, runtime: "ResearchRuntime") -> None:
        self._runtime = runtime

    async def run_plan_turn(self, plan_id: str, state: Any) -> PlanTurnResult:
        rt = self._runtime
        if rt._plan_turn.__name__ != "unavailable_plan_turn":
            return await rt._plan_turn(plan_id, state)
        plan_input = await rt._supervisor.plan_input(plan_id)
        runner = PlanRunner(
            execution=rt._execution,
            store=rt._store,
            evaluator=rt._evaluator,
            workspace=rt._git,
            branch=rt._supervisor.workspace(plan_id),
            context=ExecutionContext(
                project_root=rt._root,
                workspace_root=rt._supervisor.workspace_path(plan_id),
                environment_root=rt._root,
                experiment_id=plan_id,
            ),
            direction=plan_input.direction,
        )
        return await runner.run_turn(plan_id, state, plan_input)

    def _checkpointed_turns(self, field: str) -> int:
        """Return turns already spent by one PREPARE step, per the checkpoint.

        例：``state.prepare = {"prepare_turns": 2}`` → ``_checkpointed_turns
        ("prepare_turns") == 2``；字段缺失或不是自然数一律当 0（全新开始）。
        """
        prepare = self._runtime.state.prepare or {}
        turns = prepare.get(field)
        return turns if isinstance(turns, int) and turns > 0 else 0

    async def _checkpointed_evaluator_ref(self) -> ArtifactRef | None:
        """Return the frozen evaluator recorded at the PREPARE checkpoint, if usable.

        引用只有在 artifact store 里真取得到才算数：state 可能是从别的机器/清过缓存
        的目录拷来的，那时记着的引用是个死链，必须退回重冻而不是带着它往下走。
        例：``state.prepare = {"evaluator_ref": "sha256:e"}`` 且该引用可解析 → 返回它。
        """
        prepare = self._runtime.state.prepare or {}
        evaluator_ref = prepare.get("evaluator_ref")
        if not isinstance(evaluator_ref, str) or not evaluator_ref:
            return None
        try:
            await self._runtime._store.get_text(evaluator_ref)
        except (OSError, ValueError):
            return None
        return evaluator_ref

    async def run_prepare_phase(self) -> PrepareResult:
        rt = self._runtime
        if rt._prepare_phase is not None:
            return await rt._prepare_phase()
        if rt._provider is None:
            raise RuntimeError("PREPARE requires a registered Agent provider")
        await rt.publish_output(
            source="supervisor", channel="text", text="PREPARE: 初始化项目仓库…"
        )
        base_commit = await rt._git.init()
        workspace = await rt._git.create(base_commit, "athena/prepare", name="eda")
        # 只把 EDA 目录路径交给 supervisor 持有的持久化 state；EDA 结果不进 SEARCH。
        # 存相对项目根的路径而非绝对路径：state 才项目自包含。
        rt._state.eda_dir = str(
            Path(workspace.path).resolve().relative_to(rt._root.resolve())
        )
        rt._state.save(rt._state_path)
        await rt.publish_output(
            source="supervisor",
            channel="text",
            text=f"PREPARE: EDA 工作区 {rt._state.eda_dir} 已就绪。",
        )
        # 步骤 1：evaluator agent 在 workspaces/evaluator/ 写评估器并冻结。
        # 断点续传：上一轮已冻结过就直接沿用，这一整轮多回合 agent 不必重来。
        evaluator_ref = await self._checkpointed_evaluator_ref()
        if evaluator_ref is None:
            await rt.publish_output(
                source="supervisor", channel="text", text="PREPARE: 冻结评估器…"
            )
            evaluator_dir = rt._workspaces_root / "evaluator"
            if not rt._registry.contains("evaluator"):
                register_evaluator_agent(
                    rt._registry,
                    provider=rt._provider,
                    artifacts=rt._store,
                    workspace=evaluator_dir,
                    runtime=rt._execution,
                    extra_tools=rt.kaggle_tools("evaluator"),
                )
            evaluator_ref = await run_evaluator_plan(
                agents=rt._agents,
                scripts=rt._scripts,
                store=rt._store,
                evaluator_dir=evaluator_dir,
                execution=rt._execution,
                task=rt._task_text,
                max_turns=MAX_PLAN_TURNS,
                publish=lambda kind, ref, data: rt._events_bus.project_agent_event(
                    "evaluator", kind, ref, data
                ),
                turns_used=self._checkpointed_turns("evaluator_turns"),
                checkpoint=lambda turns: rt._supervisor.checkpoint_prepare(
                    evaluator_turns=turns
                ),
            )
            await rt._supervisor.checkpoint_prepare(evaluator_ref=evaluator_ref)
        else:
            await rt.publish_output(
                source="supervisor",
                channel="text",
                text="PREPARE: 沿用断点处已冻结的评估器。",
            )
        # 步骤 2：prepare agent 在 EDA worktree 写 experiment 产物并可信打分。
        await rt.publish_output(
            source="supervisor", channel="text", text="PREPARE: 运行 PREPARE Agent 并打分…"
        )
        if not rt._registry.contains("prepare"):
            register_prepare_agent(
                rt._registry,
                provider=rt._provider,
                artifacts=rt._store,
                workspace=Path(workspace.path),
                runtime=rt._execution,
                extra_tools=rt.kaggle_tools("prepare"),
            )
        tree_ref = await rt._store.put_text(
            json.dumps(rt.tree.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        return await run_prepare_plan(
            agents=rt._agents,
            evaluator=rt._evaluator,
            git=rt._git,
            workspace=workspace,
            execution=rt._execution,
            store=rt._store,
            evaluator_ref=evaluator_ref,
            tree_ref=tree_ref,
            task=rt._task_text,
            max_turns=MAX_PLAN_TURNS,
            publish=lambda kind, ref, data: rt._events_bus.project_agent_event(
                "prepare", kind, ref, data
            ),
            turns_used=self._checkpointed_turns("prepare_turns"),
            checkpoint=lambda turns: rt._supervisor.checkpoint_prepare(
                prepare_turns=turns
            ),
        )

    async def run_validation_phase(
        self, sota_commit: str, metric: float
    ) -> ValidationResult:
        rt = self._runtime
        if rt._validation_phase is not None:
            return await rt._validation_phase(sota_commit, metric)
        if rt._provider is None:
            raise RuntimeError("VALIDATE requires a registered Agent provider")
        evaluator_ref = rt._supervisor.evaluator_ref
        if evaluator_ref is None:
            raise RuntimeError("VALIDATE requires a frozen evaluator")
        workspace = await rt._git.create(sota_commit, "athena/validate")
        if not rt._registry.contains("validate"):
            register_validate_agent(
                rt._registry,
                provider=rt._provider,
                artifacts=rt._store,
                workspace=Path(workspace.path),
                runtime=rt._execution,
            )
        sota_id = rt.tree.best_experiment_id()
        if sota_id is None:
            raise RuntimeError("VALIDATE requires a trusted SOTA baseline")
        experiment = rt.tree.get_experiment(sota_id)
        hypothesis = rt.tree.get_hypothesis(experiment.hypothesis_id)
        sota_context = {
            "experiment_id": sota_id,
            "statement": hypothesis.statement,
            "intervention": hypothesis.intervention,
            "expected_effect": hypothesis.expected_effect,
            "commit": experiment.commit,
            "metric": experiment.eval.primary if experiment.eval else None,
        }
        frozen = ValidationInput(
            sota_commit=sota_commit,
            reference_metric=metric,
            direction=rt._direction,
            final_evaluator_ref=evaluator_ref,
            validation_key=validation_key(
                sota_commit, metric, rt._direction, evaluator_ref
            ),
            sota_context=sota_context,
        )
        result_ref = None
        if rt.state.validation is not None:
            candidate = rt.state.validation.get("result_ref")
            if isinstance(candidate, str):
                result_ref = candidate
        return await run_validation_plan(
            input=frozen,
            agents=rt._agents,
            git=rt._git,
            workspace=workspace,
            execution=rt._execution,
            evaluator=rt._evaluator,
            store=rt._store,
            independent_review=self.review_validation_diff,
            result_ref=result_ref,
            checkpoint=rt._supervisor.checkpoint_validation,
            publish=lambda kind, ref, data: rt._events_bus.project_agent_event(
                "validate", kind, ref, data
            ),
        )

    async def review_validation_diff(self, prompt: str) -> ValidationDiffReview:
        rt = self._runtime
        if rt._model is None:
            raise RuntimeError("independent validation review requires a model")
        answer = await single_turn_chat(
            prompt,
            model=rt._model,
            client=rt._client,
            system_prompt=(
                "Independently review the proposed VALIDATE diff. Accept only "
                "runtime compatibility repairs and reject changes to model, data, "
                "features, preprocessing, training, or final-label access. Return "
                'JSON only: {"accepted":true|false,"reason":"..."}.'
            ),
            max_turns=200,
        )
        return ValidationDiffReview.model_validate_json(answer)
