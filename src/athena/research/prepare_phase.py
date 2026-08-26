"""PREPARE phase orchestration.

Split out of ``PhaseRunner`` so the phase runner stays a small dispatcher and
the long PREPARE pipeline (evaluator freeze, EDA, baseline design, trusted
baseline score) lives in one focused module.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from athena.agents.ideator_agent import (
    BASELINE_IDEATOR_PROFILE,
    register_ideator_agent,
)
from athena.agents.prepare_agent import (
    PREPARE_EDA_AGENT_ID,
    PREPARE_EDA_AGENT_TYPE,
    register_evaluator_agent,
    register_prepare_agent,
    register_prepare_eda_agent,
)
from athena.agents.supervisor_agent import MAX_PLAN_TURNS
from athena.research.eda_todo import run_eda_todos
from athena.research.splitter import materialize_csv_split
from athena.research.supervisor.prepare import (
    EVALUATOR_AGENT_ID,
    EVALUATOR_PLAN_ID,
    FINAL_EVALUATOR_AGENT_ID,
    FINAL_EVALUATOR_PLAN_ID,
    PrepareResult,
    run_evaluator_plan,
    run_prepare_plan,
)

logger = logging.getLogger(__name__)

HandoffFn = Callable[..., Awaitable[str]]


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


async def _freeze_evaluator(
    rt: Any,
    *,
    directory_name: str,
    agent_id: str,
    plan_id: str,
    task: str,
    label: str,
) -> str:
    """Run one evaluator agent in a dedicated directory and freeze its bundle."""
    evaluator_dir = rt.workspaces_root / directory_name
    if not rt.registry.contains("evaluator"):
        register_evaluator_agent(
            rt.registry,
            provider=rt.provider,
            artifacts=rt.store,
            workspace=evaluator_dir,
            runtime=rt.execution,
            extra_tools=rt.kaggle_tools("evaluator"),
        )
    return await run_evaluator_plan(
        agents=rt.agents,
        scripts=rt.scripts,
        store=rt.store,
        evaluator_dir=evaluator_dir,
        execution=rt.execution,
        task=task,
        max_turns=MAX_PLAN_TURNS,
        publish=lambda kind, ref, data: rt.events.project_agent_event(
            label, kind, ref, data
        ),
        agent_id=agent_id,
        plan_id=plan_id,
    )


async def run_prepare_phase(
    runtime: Any, run_handoff_agent: HandoffFn
) -> PrepareResult:
    """Run the PREPARE phase and return the trusted baseline result."""
    rt = runtime
    if rt.prepare_phase is not None:
        return await rt.prepare_phase()
    if rt.provider is None:
        raise RuntimeError("PREPARE requires a registered Agent provider")
    await rt.publish_output(
        source="supervisor", channel="text", text="PREPARE: 初始化项目仓库…"
    )
    base_commit = await rt.git.init()
    workspace = await rt.git.create(base_commit, "athena/prepare", name="eda")
    # Only hand the EDA directory to the Supervisor-owned durable state; EDA
    # results do not enter SEARCH. Store a project-relative path, not absolute.
    rt.state.eda_dir = str(
        Path(workspace.path).resolve().relative_to(rt.root.resolve())
    )
    rt.state.save(rt.state_path)
    await rt.publish_output(
        source="supervisor",
        channel="text",
        text=f"PREPARE: EDA 工作区 {rt.state.eda_dir} 已就绪（绝对路径 {Path(workspace.path).resolve()}）。",
    )

    # Optional platform-level data split: when the task names a local CSV,
    # generate train/search/final files so the evaluator does not split itself.
    evaluator_task = rt.task_text
    if rt.config.dataset_path is not None and rt.config.target_column is not None:
        split_dir = rt.workspaces_root / "data_split"
        materialize_csv_split(
            rt.config.dataset_path,
            split_dir,
            rt.config.target_column,
            search_frac=0.2,
            final_frac=0.2,
            seed=rt.config.split_seed,
        )
        await rt.publish_output(
            source="supervisor",
            channel="text",
            text=f"PREPARE: 平台已生成数据划分 {split_dir.resolve()}。",
        )
        evaluator_task = (
            f"{rt.task_text}\n\n"
            f"The platform has already split the dataset into train/search/final "
            f"files under {split_dir.resolve()}.\n"
            "Do NOT create your own split. Build evaluate.py using "
            "search_labels.csv as the trusted search labels, and keep "
            "final_labels.csv hidden from SEARCH."
        )

    # Step 1: search evaluator. Reuse a checkpointed frozen bundle when present.
    evaluator_dir = rt.workspaces_root / "evaluator"
    evaluator_ref = rt.supervisor.evaluator_ref
    if evaluator_ref is not None:
        try:
            await rt.store.get_text(evaluator_ref)
        except Exception:
            evaluator_ref = None
    if evaluator_ref is None:
        await rt.publish_output(
            source="supervisor", channel="text", text="PREPARE: 冻结评估器…"
        )
        evaluator_ref = await _freeze_evaluator(
            rt,
            directory_name="evaluator",
            agent_id=EVALUATOR_AGENT_ID,
            plan_id=EVALUATOR_PLAN_ID,
            task=evaluator_task,
            label="evaluator",
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

    # Step 1b: final evaluator (hidden, VALIDATE only). It is frozen separately
    # so the SEARCH evaluator's labels are never reused as the final test.
    final_evaluator_dir = rt.workspaces_root / "final_evaluator"
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
        final_evaluator_ref = await _freeze_evaluator(
            rt,
            directory_name="final_evaluator",
            agent_id=FINAL_EVALUATOR_AGENT_ID,
            plan_id=FINAL_EVALUATOR_PLAN_ID,
            task=(
                f"{evaluator_task}\n\nYou are building the FINAL evaluator. "
                "Use a held-out split disjoint from the SEARCH evaluator's "
                "split. This evaluator is hidden from SEARCH and used only "
                "by VALIDATE."
            ),
            label="final_evaluator",
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

    # Step 2a: EDA orchestrator -> todo workers -> finalize.
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
        await run_handoff_agent(
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
            await run_handoff_agent(
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

    # Step 2b: baseline ideator reads EDA handoff and writes BASELINE_DESIGN.md.
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
            await run_handoff_agent(
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

    # Step 3: prepare agent implements the baseline and receives a trusted score.
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
