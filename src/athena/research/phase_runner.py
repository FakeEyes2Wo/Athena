"""Phase execution for the research lifecycle (PREPARE / VALIDATE / plan turn).

拆自 ``ResearchRuntime``：把 phase 编排（run_plan_turn / run_prepare_phase /
run_validation_phase / review_validation_diff）集中到一个组合单元。持有
``runtime`` 引用以访问组合根的共享基础设施；避免在构造期重复注入十余个
可变字段。
"""

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

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
    FINAL_EVALUATOR_AGENT_ID,
    FINAL_EVALUATOR_PLAN_ID,
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
        if rt.plan_turn is not None:
            return await rt.plan_turn(plan_id, state)
        plan_input = await rt.supervisor.plan_input(plan_id)
        runner = PlanRunner(
            execution=rt.execution,
            store=rt.store,
            evaluator=rt.evaluator,
            workspace=rt.git,
            branch=rt.supervisor.workspace(plan_id),
            context=ExecutionContext(
                project_root=rt.root,
                workspace_root=rt.supervisor.workspace_path(plan_id),
                environment_root=rt.root,
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
            if rt.agents.has_agent(agent_id):
                run_id = await rt.agents.followup(agent_id, request)
            else:
                _id, run_id = await rt.agents.create_root(
                    agent_type, request, agent_id=agent_id, name=agent_id
                )

            def publish(kind: str, ref: str, data: dict | None = None) -> None:
                """Forward one agent journal event to the runtime event bus."""
                events_bus = getattr(rt, "events", None)
                if events_bus is not None:
                    events_bus.project_agent_event(agent_id, kind, ref, data)

            summary = await wait_run_events(rt.agents, run_id, publish)
            result = await load_agent_result(summary, rt.store, HandoffResult)
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
                    await rt.agents.reap(agent_id)
                except Exception:  # noqa: BLE001,S110 - GC must never mask handoff failure
                    pass

    async def run_prepare_phase(self) -> PrepareResult:
        """Run the PREPARE phase and return the trusted baseline result."""
        rt = self._runtime
        if rt.prepare_phase is not None:
            return await rt.prepare_phase()
        if rt.provider is None:
            raise RuntimeError("PREPARE requires a registered Agent provider")
        await rt.publish_output(
            source="supervisor", channel="text", text="PREPARE: 初始化项目仓库…"
        )
        base_commit = await rt.git.init()
        workspace = await rt.git.create(base_commit, "athena/prepare", name="eda")
        # 只把 EDA 目录路径交给 supervisor 持有的持久化 state；EDA 结果不进 SEARCH。
        # 存相对项目根的路径而非绝对路径：state 才项目自包含。
        rt.state.eda_dir = str(
            Path(workspace.path).resolve().relative_to(rt.root.resolve())
        )
        rt.state.save(rt.state_path)
        await rt.publish_output(
            source="supervisor",
            channel="text",
            text=f"PREPARE: EDA 工作区 {rt.state.eda_dir} 已就绪（绝对路径 {Path(workspace.path).resolve()}）。",
        )
        # 步骤 1：evaluator agent 在 workspaces/evaluator/ 写评估器并冻结；
        # 断点续传时若已有可解析的 frozen bundle，直接复用不重跑。
        evaluator_ref = rt.supervisor.evaluator_ref
        if evaluator_ref is not None:
            try:
                await rt.store.get_text(evaluator_ref)
            except Exception:
                # artifact 缺失或损坏 → 重新冻结评估器
                evaluator_ref = None
        if evaluator_ref is None:
            await rt.publish_output(
                source="supervisor", channel="text", text="PREPARE: 冻结评估器…"
            )
            evaluator_dir = rt.workspaces_root / "evaluator"
            if not rt.registry.contains("evaluator"):
                register_evaluator_agent(
                    rt.registry,
                    provider=rt.provider,
                    artifacts=rt.store,
                    workspace=evaluator_dir,
                    runtime=rt.execution,
                    extra_tools=rt.kaggle_tools("evaluator"),
                )
            evaluator_ref = await run_evaluator_plan(
                agents=rt.agents,
                scripts=rt.scripts,
                store=rt.store,
                evaluator_dir=evaluator_dir,
                execution=rt.execution,
                task=rt.task_text,
                max_turns=MAX_PLAN_TURNS,
                publish=lambda kind, ref, data: rt.events.project_agent_event(
                    "evaluator", kind, ref, data
                ),
            )
            await rt.supervisor.checkpoint_evaluator(evaluator_ref)
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
        # 步骤 1b：final evaluator（隐藏、仅 VALIDATE 使用）。它与搜索 evaluator
        # 分开冻结，避免把 SEARCH 用过的同一份标签再次称为 final test。
        final_evaluator_ref = rt.supervisor.final_evaluator_ref
        if final_evaluator_ref is not None:
            try:
                await rt.store.get_text(final_evaluator_ref)
            except Exception:
                final_evaluator_ref = None
        if final_evaluator_ref is None:
            await rt.publish_output(
                source="supervisor",
                channel="text",
                text="PREPARE: 冻结 final evaluator…",
            )
            final_evaluator_dir = rt.workspaces_root / "final_evaluator"
            if not rt.registry.contains("evaluator"):
                register_evaluator_agent(
                    rt.registry,
                    provider=rt.provider,
                    artifacts=rt.store,
                    workspace=final_evaluator_dir,
                    runtime=rt.execution,
                    extra_tools=rt.kaggle_tools("evaluator"),
                )
            final_evaluator_ref = await run_evaluator_plan(
                agents=rt.agents,
                scripts=rt.scripts,
                store=rt.store,
                evaluator_dir=final_evaluator_dir,
                execution=rt.execution,
                task=(
                    f"{rt.task_text}\n\nYou are building the FINAL evaluator. "
                    "Use a held-out split disjoint from the SEARCH evaluator's "
                    "split. This evaluator is hidden from SEARCH and used only "
                    "by VALIDATE."
                ),
                max_turns=MAX_PLAN_TURNS,
                publish=lambda kind, ref, data: rt.events.project_agent_event(
                    "final_evaluator", kind, ref, data
                ),
                agent_id=FINAL_EVALUATOR_AGENT_ID,
                plan_id=FINAL_EVALUATOR_PLAN_ID,
            )
            await rt.supervisor.checkpoint_final_evaluator(final_evaluator_ref)
            await rt.publish_output(
                source="supervisor",
                channel="text",
                text=(
                    "PREPARE: final evaluator 产物目录 "
                    f"{final_evaluator_dir.resolve()}。"
                ),
            )
        # 步骤 2a：EDA orchestrator → todo workers → finalize。
        eda_ok = True
        try:
            await rt.publish_output(
                source="supervisor",
                channel="text",
                text="PREPARE: 生成 EDA_TODO.md…",
            )
            if not rt.registry.contains(PREPARE_EDA_AGENT_TYPE):
                register_prepare_eda_agent(
                    rt.registry,
                    provider=rt.provider,
                    artifacts=rt.store,
                    workspace=Path(workspace.path),
                    runtime=rt.execution,
                    extra_tools=rt.kaggle_tools("prepare"),
                )
            await self._run_handoff_agent(
                agent_id=PREPARE_EDA_AGENT_ID,
                agent_type=PREPARE_EDA_AGENT_TYPE,
                workspace=str(workspace.path),
                output_file="EDA_TODO.md",
                content=rt.task_text,
            )
            failed = await run_eda_todos(
                agents=rt.agents,
                store=rt.store,
                workspace=Path(workspace.path),
                project_event=lambda aid, kind, ref, data: rt.events.project_agent_event(
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
                await rt.agents.reap(PREPARE_EDA_AGENT_ID)
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
                if not rt.registry.contains(BASELINE_IDEATOR_PROFILE.agent_type):
                    register_ideator_agent(
                        rt.registry,
                        provider=rt.provider,
                        artifacts=rt.store,
                        workspace=Path(workspace.path),
                        runtime=rt.execution,
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
                        f"{rt.task_text}\n\nRead EDA_HANDOFF.md and write "
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
        if not rt.registry.contains("prepare"):
            register_prepare_agent(
                rt.registry,
                provider=rt.provider,
                artifacts=rt.store,
                workspace=Path(workspace.path),
                runtime=rt.execution,
                extra_tools=rt.kaggle_tools("prepare"),
            )
        tree_ref = await rt.store.put_text(
            json.dumps(rt.tree.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        return await run_prepare_plan(
            agents=rt.agents,
            evaluator=rt.evaluator,
            git=rt.git,
            workspace=workspace,
            execution=rt.execution,
            store=rt.store,
            evaluator_ref=evaluator_ref,
            tree_ref=tree_ref,
            task=rt.task_text,
            max_turns=MAX_PLAN_TURNS,
            publish=lambda kind, ref, data: rt.events.project_agent_event(
                "prepare", kind, ref, data
            ),
        )

    async def run_validation_phase(
        self, sota_commit: str, metric: float
    ) -> ValidationResult:
        """Run the VALIDATE phase and return the validation result."""
        rt = self._runtime
        if rt.validation_phase is not None:
            return await rt.validation_phase(sota_commit, metric)
        if rt.provider is None:
            raise RuntimeError("VALIDATE requires a registered Agent provider")
        evaluator_ref = rt.supervisor.evaluator_ref
        if evaluator_ref is None:
            raise RuntimeError("VALIDATE requires a frozen evaluator")
        final_evaluator_ref = getattr(rt.supervisor, "final_evaluator_ref", None)
        if final_evaluator_ref is None:
            # Legacy projects do not have a separate final evaluator yet. Keep
            # working but make the limitation explicit instead of silently
            # pretending the search evaluator is an unseen final test.
            final_evaluator_ref = evaluator_ref
            logger.warning(
                "VALIDATE has no final_evaluator_ref; using the search evaluator "
                "as final. Re-run PREPARE with final-evaluator support for a "
                "true unseen final test."
            )
        workspace = await rt.git.create(sota_commit, "athena/validate")
        if not rt.registry.contains("validate"):
            register_validate_agent(
                rt.registry,
                provider=rt.provider,
                artifacts=rt.store,
                workspace=Path(workspace.path),
                runtime=rt.execution,
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
            direction=rt.direction,
            final_evaluator_ref=final_evaluator_ref,
            validation_key=validation_key(
                sota_commit, metric, rt.direction, final_evaluator_ref
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
            agents=rt.agents,
            git=rt.git,
            workspace=workspace,
            execution=rt.execution,
            evaluator=rt.evaluator,
            store=rt.store,
            independent_review=self.review_validation_diff,
            result_ref=result_ref,
            checkpoint=rt.supervisor.checkpoint_validation,
            publish=lambda kind, ref, data: rt.events.project_agent_event(
                "validate", kind, ref, data
            ),
        )

    async def review_validation_diff(self, prompt: str) -> ValidationDiffReview:
        """Independently review and accept/reject a proposed validation diff."""
        rt = self._runtime
        if rt.model is None:
            raise RuntimeError("independent validation review requires a model")
        answer = await single_turn_chat(
            prompt,
            model=rt.model,
            client=rt.client,
            system_prompt=(
                "Independently review the proposed VALIDATE diff. Accept only "
                "runtime compatibility repairs and reject changes to model, data, "
                "features, preprocessing, training, or final-label access. Return "
                'JSON only: {"accepted":true|false,"reason":"..."}.'
            ),
            max_turns=200,
        )
        return ValidationDiffReview.model_validate_json(answer)
