"""Phase execution for the research lifecycle (PREPARE / VALIDATE / plan turn).

拆自 ``ResearchRuntime``：把 phase 编排（run_plan_turn / run_prepare_phase /
run_validation_phase / review_validation_diff）集中到一个组合单元。持有
``runtime`` 引用以访问组合根的共享基础设施；避免在构造期重复注入十余个
可变字段。
"""

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athena.agents.ideator_agent import (
    BASELINE_IDEATOR_PROFILE,
    HandoffResult,
    register_ideator_agent,
)
from athena.agents.prepare_agent import register_evaluator_agent, register_prepare_agent
from athena.agents.prepare_eda_agent import (
    PREPARE_EDA_AGENT_ID,
    PREPARE_EDA_AGENT_TYPE,
    register_prepare_eda_agent,
)
from athena.agents.supervisor_agent import MAX_PLAN_TURNS
from athena.agents.validate_agent import register_validate_agent
from athena.execution.runtime import ExecutionContext
from athena.research.contracts import ValidationResult
from athena.research.eda_todo import run_eda_todos
from athena.research.supervisor.experiment import (
    PlanRunner,
    PlanTurnResult,
    load_agent_result,
)
from athena.research.supervisor.plans import wait_run_events
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


def _write_fallback_eda(workspace: Path) -> None:
    """Write minimal EDA entry files when the EDA pipeline fails."""
    workspace.mkdir(parents=True, exist_ok=True)
    if not (workspace / "EDA_INDEX.md").exists():
        (workspace / "EDA_INDEX.md").write_text(
            "# EDA Index\n\nEDA generation failed; see logs.\n", encoding="utf-8"
        )
    if not (workspace / "EDA_HANDOFF.md").exists():
        (workspace / "EDA_HANDOFF.md").write_text(
            "# EDA Handoff\n\nEDA generation failed; baseline should explore the raw data.\n",
            encoding="utf-8",
        )
    _write_missing_report_placeholders(workspace)


def _write_missing_report_placeholders(workspace: Path) -> None:
    """Create placeholder files for every EDA_REPORT_*.md named in EDA_TODO.md."""
    todo_path = workspace / "EDA_TODO.md"
    if not todo_path.is_file():
        return
    pattern = re.compile(r"^\- \[[ x]\]\s+.+?\s*->\s*([^\s#]+)")
    for line in todo_path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        name = match.group(1)
        if name in {"EDA_INDEX.md", "EDA_HANDOFF.md"}:
            continue
        target = workspace / name
        if not target.exists():
            target.write_text(
                f"# {name}\n\nEDA generation failed or was skipped.\n",
                encoding="utf-8",
            )


class PhaseRunner:
    """Run PREPARE / VALIDATE / plan phases via the runtime's shared infra."""

    def __init__(self, runtime: "ResearchRuntime") -> None:
        self._runtime = runtime

    async def run_plan_turn(self, plan_id: str, state: Any) -> PlanTurnResult:
        """Execute one Plan turn through the configured plan runner."""
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

    async def _run_handoff_agent(
        self,
        *,
        agent_id: str,
        agent_type: str,
        workspace: str,
        output_file: str,
        content: str,
        reap_after: bool = False,
    ) -> str:
        """Run one handoff-producing agent and return the output file text.

        ``reap_after`` should be True only on the agent's last use; it releases
        the one-shot thread and facade metadata immediately.
        """
        rt = self._runtime
        request = {"content": content, "context_refs": []}
        try:
            if rt._agents.has_agent(agent_id):
                run_id = await rt._agents.followup(agent_id, request)
            else:
                _id, run_id = await rt._agents.create_root(
                    agent_type, request, agent_id=agent_id, name=agent_id
                )

            def publish(kind: str, ref: str, data: dict | None = None) -> None:
                """Forward one agent journal event to the runtime event bus."""
                events_bus = getattr(rt, "_events_bus", None)
                if events_bus is not None:
                    events_bus.project_agent_event(agent_id, kind, ref, data)

            summary = await wait_run_events(rt._agents, run_id, publish)
            result = await load_agent_result(summary, rt._store, HandoffResult)
            path = Path(workspace) / output_file
            # 文件已落盘但结果解析失败时，仍视为成功，避免“文件存在却标红”。
            if result is None:
                if path.is_file():
                    return path.read_text(encoding="utf-8")
                raise RuntimeError(summary.error or f"{agent_type} handoff agent failed")
            if not path.is_file():
                raise RuntimeError(f"{agent_type} did not write {output_file}")
            return path.read_text(encoding="utf-8")
        finally:
            if reap_after:
                try:
                    await rt._agents.reap(agent_id)
                except Exception:  # noqa: BLE001,S110 - GC must never mask handoff failure
                    pass

    async def run_prepare_phase(self) -> PrepareResult:
        """Run the PREPARE phase and return the trusted baseline result."""
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
        rt.state.eda_dir = str(
            Path(workspace.path).resolve().relative_to(rt._root.resolve())
        )
        rt.state.save(rt._state_path)
        await rt.publish_output(
            source="supervisor",
            channel="text",
            text=f"PREPARE: EDA 工作区 {rt.state.eda_dir} 已就绪（绝对路径 {Path(workspace.path).resolve()}）。",
        )
        # 步骤 1：evaluator agent 在 workspaces/evaluator/ 写评估器并冻结；
        # 断点续传时若已有可解析的 frozen bundle，直接复用不重跑。
        evaluator_ref = rt._supervisor.evaluator_ref
        if evaluator_ref is not None:
            try:
                await rt._store.get_text(evaluator_ref)
            except Exception:
                # artifact 缺失或损坏 → 重新冻结评估器
                evaluator_ref = None
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
            )
            await rt._supervisor.checkpoint_evaluator(evaluator_ref)
            await rt.publish_output(
                source="supervisor",
                channel="text",
                text=f"PREPARE: evaluator 产物目录 {evaluator_dir.resolve()}。",
            )
            handoff_path = evaluator_dir / "HANDOFF.md"
            if handoff_path.is_file():
                for line in handoff_path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if stripped.lower().startswith("validation_sample_count:"):
                        await rt.publish_output(
                            source="supervisor",
                            channel="text",
                            text=f"PREPARE: {stripped}",
                        )
                        break
        else:
            await rt.publish_output(
                source="supervisor",
                channel="text",
                text="PREPARE: 复用已冻结的评估器断点，跳过 evaluator Agent。",
            )
        # 步骤 2a：EDA orchestrator → todo workers → finalize。
        eda_ok = True
        try:
            await rt.publish_output(
                source="supervisor",
                channel="text",
                text="PREPARE: 生成 EDA_TODO.md…",
            )
            if not rt._registry.contains(PREPARE_EDA_AGENT_TYPE):
                register_prepare_eda_agent(
                    rt._registry,
                    provider=rt._provider,
                    artifacts=rt._store,
                    workspace=Path(workspace.path),
                    runtime=rt._execution,
                    extra_tools=rt.kaggle_tools("prepare"),
                )
            await self._run_handoff_agent(
                agent_id=PREPARE_EDA_AGENT_ID,
                agent_type=PREPARE_EDA_AGENT_TYPE,
                workspace=str(workspace.path),
                output_file="EDA_TODO.md",
                content=rt._task_text,
            )
            failed = await run_eda_todos(
                agents=rt._agents,
                store=rt._store,
                workspace=Path(workspace.path),
                project_event=lambda aid, kind, ref, data: rt._events_bus.project_agent_event(
                    aid, kind, ref, data
                ),
            )
            if failed:
                await rt.publish_output(
                    source="supervisor",
                    channel="error",
                    text=f"EDA todo failed: {failed}; writing fallback EDA files.",
                )
                _write_fallback_eda(Path(workspace.path))
                eda_ok = False
            else:
                await rt.publish_output(
                    source="supervisor",
                    channel="text",
                    text="PREPARE: 汇总 EDA 报告…",
                )
                await self._run_handoff_agent(
                    agent_id=PREPARE_EDA_AGENT_ID,
                    agent_type=PREPARE_EDA_AGENT_TYPE,
                    workspace=str(workspace.path),
                    output_file="EDA_INDEX.md",
                    content=(
                        "Finalize EDA: read all EDA_REPORT_*.md and write "
                        "EDA_INDEX.md and EDA_HANDOFF.md."
                    ),
                    reap_after=True,
                )
                eda_dir = Path(workspace.path)
                if (
                    not (eda_dir / "EDA_INDEX.md").is_file()
                    or not (eda_dir / "EDA_HANDOFF.md").is_file()
                ):
                    _write_fallback_eda(eda_dir)
                for report in sorted(eda_dir.glob("EDA_REPORT_*.md")):
                    await rt.publish_output(
                        source="supervisor",
                        channel="text",
                        text=f"EDA report: {report.resolve()}",
                    )
        except Exception as error:
            await rt.publish_output(
                source="supervisor",
                channel="error",
                text=f"EDA handoff failed ({error}); writing fallback EDA files.",
            )
            _write_fallback_eda(Path(workspace.path))
            eda_ok = False
            try:
                await rt._agents.reap(PREPARE_EDA_AGENT_ID)
            except Exception:  # noqa: BLE001,S110 - GC must never mask EDA failure
                pass
        # 步骤 2b：baseline_ideator 读 EDA handoff，写 BASELINE_DESIGN.md。
        if eda_ok:
            try:
                await rt.publish_output(
                    source="supervisor",
                    channel="text",
                    text="PREPARE: 生成 BASELINE_DESIGN.md…",
                )
                if not rt._registry.contains(BASELINE_IDEATOR_PROFILE.agent_type):
                    register_ideator_agent(
                        rt._registry,
                        provider=rt._provider,
                        artifacts=rt._store,
                        workspace=Path(workspace.path),
                        runtime=rt._execution,
                        extra_tools=rt.ideator_tools(),
                        gated=True,
                        profile=BASELINE_IDEATOR_PROFILE,
                    )
                await self._run_handoff_agent(
                    agent_id=BASELINE_IDEATOR_PROFILE.agent_type,
                    agent_type=BASELINE_IDEATOR_PROFILE.agent_type,
                    workspace=str(workspace.path),
                    output_file="BASELINE_DESIGN.md",
                    content=(
                        f"{rt._task_text}\n\nRead EDA_HANDOFF.md and write "
                        "BASELINE_DESIGN.md."
                    ),
                    reap_after=True,
                )
            except Exception as error:
                await rt.publish_output(
                    source="supervisor",
                    channel="error",
                    text=f"Baseline design failed ({error}); prepare falls back to task-only.",
                )
        else:
            await rt.publish_output(
                source="supervisor",
                channel="text",
                text="PREPARE: 因 EDA 失败跳过 BASELINE_DESIGN，prepare 将基于任务原文降级。",
            )
        # 步骤 3：prepare agent 按 BASELINE_DESIGN.md 实现并可信打分。
        await rt.publish_output(
            source="supervisor",
            channel="text",
            text="PREPARE: 运行 PREPARE Agent 并打分…",
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
        )

    async def run_validation_phase(
        self, sota_commit: str, metric: float
    ) -> ValidationResult:
        """Run the VALIDATE phase and return the validation result."""
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
        """Independently review and accept/reject a proposed validation diff."""
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
