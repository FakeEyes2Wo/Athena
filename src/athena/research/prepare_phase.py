"""PREPARE phase orchestration.

Split out of ``PhaseRunner`` so the phase runner stays a small dispatcher and
the long PREPARE pipeline (evaluator freeze, EDA, baseline design, trusted
baseline score) lives in one focused module.
"""

import csv
import json
import logging
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from athena.agents.ideator_agent import (
    BASELINE_IDEATOR_PROFILE,
    register_ideator_agent,
)
from athena.agents.prepare_agent import (
    EVALUATOR_AGENT_TYPE,
    PREPARE_EDA_AGENT_ID,
    PREPARE_EDA_AGENT_TYPE,
    register_evaluator_agent,
    register_prepare_agent,
    register_prepare_eda_agent,
)
from athena.agents.supervisor_agent import MAX_PLAN_TURNS
from athena.research.eda_todo import run_eda_todos
from athena.research.splitter import SplitSpec, materialize_csv_split
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

@dataclass(frozen=True)
class PlatformSplit:
    """Value object describing a platform-owned train/search/final CSV split.

    This is the post-``materialize_csv_split`` shape: the platform owns the
    split, so candidates never have to invent one or accidentally train on
    held-out rows.
    """

    split_dir: Path
    train_csv: Path
    search_features_csv: Path
    search_labels_csv: Path
    final_features_csv: Path
    final_labels_csv: Path
    dataset_path: Path
    target_column: str
    group_column: str | None = None

    @property
    def grouping(self) -> str:
        if self.group_column is None:
            return ""
        return (
            f"Rows were kept together by {self.group_column!r}, so no group "
            "spans two splits."
        )

    def data_contract(self) -> "DataContract":
        return DataContract(
            train_csv=self.train_csv,
            predict_features_csv=self.search_features_csv,
            dataset_path=self.dataset_path,
            group_column=self.group_column,
        )


@dataclass(frozen=True)
class DataContract:
    """Platform-owned data rules, rendered consistently for every agent role.

    PREPARE needs the same facts in three slightly different phrasings: the
    evaluator must build against the platform split, the baseline must train
    only on the platform train file, and every later SEARCH candidate must see
    the same constraint through ``state.data_contract`` (because candidates do
    not receive the task text via ``context_refs``).
    """

    train_csv: Path
    predict_features_csv: Path
    dataset_path: Path
    group_column: str | None = None

    @property
    def grouping(self) -> str:
        if self.group_column is None:
            return ""
        return (
            f"Rows were kept together by {self.group_column!r}, so no group "
            "spans two splits."
        )

    @property
    def moving_target(self) -> str:
        """The rule candidates break by pairing the predict file with a fixed one.

        Reading ``ATHENA_PREDICT_FEATURES`` is necessary but not sufficient. On
        2026-08-31 a candidate did read it for its features and then scored those
        predictions against a hardcoded ``search_labels.csv``; under VALIDATE the
        features became the held-out split and the labels did not, so it died on
        ``inconsistent numbers of samples: [169725, 169965]`` and took a
        five-hour run with it. Tuning on the search split is legitimate -- what
        is not is assuming the rows you are asked to predict *are* that split.
        """
        return (
            "That variable is the only thing that moves between SEARCH and "
            "VALIDATE. Every other path you read stays exactly where it is, so "
            "never pair the two: do not score, index, align, or concatenate "
            "predictions made from ATHENA_PREDICT_FEATURES against any fixed "
            "label file, row count, or saved index. If you want a metric or a "
            "decision threshold from the search split, load that split's "
            "features under their own name and predict them separately -- the "
            "row counts differ, and a script that conflates them raises "
            "'inconsistent numbers of samples' the moment VALIDATE re-runs it."
        )

    def candidate_task(self, task: str) -> str:
        """Render the task text handed to baseline/model-writing agents."""
        return (
            f"{task}\n\n"
            f"The platform owns the data split. Train ONLY on "
            f"{self.train_csv.resolve()}. {self.grouping}\n"
            f"Do NOT read {self.dataset_path} for training, and do NOT use "
            "any other split of it you may find beside it. The rows you are "
            "scored on are drawn from that same file, so fitting on it means "
            "being scored on rows you already saw, and the metric stops "
            "measuring skill.\n"
            f"{self.predict_features_csv.resolve()} holds exactly the "
            "rows to predict, with labels withheld. Read that path from the "
            "environment variable ATHENA_PREDICT_FEATURES (os.environ) rather "
            "than hardcoding it: VALIDATE re-runs your unchanged command with "
            "the variable pointing at the held-out split, and a hardcoded path "
            "makes your result unscoreable there.\n"
            f"{self.moving_target}"
        )

    def evaluator_task(self, task: str) -> str:
        """Render the task text handed to the evaluator-building agent."""
        return (
            f"{task}\n\n"
            f"The platform has already split the dataset into train/search/final "
            f"files under {self.train_csv.parent.resolve()}. {self.grouping}\n"
            "Do NOT create your own split. Build evaluate.py using "
            "search_labels.csv as the trusted search labels, and keep "
            "final_labels.csv hidden from SEARCH."
        )

    def contract_text(self) -> str:
        """Render the durable contract stored in ``ResearchState.data_contract``."""
        return (
            f"Train ONLY on {self.train_csv.resolve()}. {self.grouping}\n"
            f"Do NOT read {self.dataset_path} for training, and do NOT use "
            "any other split of it you may find beside it. The rows you are "
            "scored on are drawn from that same file, so fitting on it means "
            "being scored on rows you already saw.\n"
            "Predict exactly the rows in the CSV named by the environment "
            "variable ATHENA_PREDICT_FEATURES (labels withheld); during SEARCH "
            f"that is {self.predict_features_csv.resolve()}. Read it "
            "from os.environ, do not hardcode it -- VALIDATE re-runs this very "
            "command with the variable pointing at the held-out split, and a "
            "hardcoded path silently produces predictions for rows nobody asked "
            "for.\n"
            f"{self.moving_target}"
        )

    def prompt_block(self) -> str:
        """Render the Supervisor prompt block for the durable contract text."""
        from athena.research.supervisor.experiment import data_contract_block

        return data_contract_block(self.contract_text())



def _label_row_ids(labels_csv: Path) -> set[str]:
    """Row ids a frozen evaluator scores against, or an empty set if unreadable."""
    try:
        with labels_csv.open(encoding="utf-8-sig", newline="") as handle:
            return {
                row["__athena_row_id"]
                for row in csv.DictReader(handle)
                if row.get("__athena_row_id")
            }
    except (OSError, ValueError, KeyError):
        return set()


def _assert_evaluator_splits_are_disjoint(
    search_labels: Path, final_labels: Path
) -> None:
    """Fail PREPARE if the two frozen evaluators score the same rows.

    The whole point of freezing two evaluators is that SEARCH never sees the
    rows VALIDATE will score on. Nothing checked it.

    Real run (2026-08-29): the shared ``evaluator`` agent type bound its
    workspace once, so the FINAL evaluator's file tools were still rooted at the
    SEARCH evaluator's directory. Its ``labels.csv`` write landed inside the
    already-frozen SEARCH evaluator and replaced those labels with the held-out
    split. SEARCH would then have been scored on exactly the rows VALIDATE was
    holding back, and every number the run produced would have been meaningless
    -- silently, because the scores stay perfectly plausible.

    That specific bug is fixed in ``_run_evaluator_agent``, but this check is
    the standing guarantee: ``shell_command`` is not sandboxed, so an agent can
    still write into a directory that is not its own, and this is the property
    that has to hold regardless of how it got broken.
    """
    search_ids = _label_row_ids(search_labels)
    final_ids = _label_row_ids(final_labels)
    if not search_ids or not final_ids:
        # A missing or unreadable labels file is the freeze step's problem, not
        # this check's; do not turn it into a confusing isolation error.
        return
    overlap = search_ids & final_ids
    if overlap:
        raise RuntimeError(
            "SEARCH and FINAL evaluators score overlapping rows "
            f"({len(overlap)} of {len(final_ids)} final rows). The held-out "
            "split is not held out. Check whether an agent wrote outside its "
            f"own workspace: {search_labels} vs {final_labels}"
        )


_PLACEHOLDER_MARK = "EDA generation failed or was skipped."


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
                f"# {name}\n\n{_PLACEHOLDER_MARK}\n",
                encoding="utf-8",
            )


def _usable_eda_reports(workspace: Path) -> list[Path]:
    """真正有内容的 EDA_REPORT_*.md（排除占位符）。

    单份报告写失败不该让另外几份陪葬——它们就在盘上，只是原来没人读。
    """
    usable: list[Path] = []
    for report in sorted(workspace.glob("EDA_REPORT_*.md")):
        try:
            body = report.read_text(encoding="utf-8")
        except OSError:
            continue
        if _PLACEHOLDER_MARK not in body:
            usable.append(report)
    return usable


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


async def _run_evaluator_agent(
    rt: Any,
    *,
    directory_name: str,
    agent_id: str,
    plan_id: str,
    task: str,
    label: str,
) -> str:
    """Run one evaluator agent in a dedicated directory and write its README freeze marker."""
    evaluator_dir = rt.workspaces_root / directory_name
    evaluator_dir.mkdir(parents=True, exist_ok=True)
    # The factory binds its workspace at registration time, and both evaluators
    # share the agent type "evaluator". Registering only when the type is absent
    # therefore left the FINAL evaluator's read_file/write_file rooted at the
    # SEARCH evaluator's directory.
    #
    # Real run (2026-08-29): the final-evaluator agent wrote labels.csv "into its
    # workspace" exactly as told, and the platform put it inside the already
    # frozen SEARCH evaluator -- replacing its labels with the held-out split.
    # Meanwhile its own directory stayed empty, so every submit was rejected for
    # a missing metric.json until the turn budget ran out. Re-bind per run.
    if rt.registry.contains(EVALUATOR_AGENT_TYPE):
        rt.registry.unregister(EVALUATOR_AGENT_TYPE)
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
        ask_user=getattr(rt, "ask_user", None),
        agent_id=agent_id,
        plan_id=plan_id,
    )


async def _prepare_workspace(runtime: Any) -> Any:
    """Initialize the PREPARE EDA workspace and persist its project-relative path."""
    rt = runtime
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
    return workspace


async def _prepare_platform_split(runtime: Any) -> DataContract | None:
    """Materialize and persist the platform-owned split when the CLI names a CSV.

    Returns the value object used to render evaluator/candidate prompts and the
    durable ``state.data_contract`` string. Returns ``None`` for directory or
    Kaggle/URL datasets, preserving the old no-platform-split path.
    """
    rt = runtime
    if rt.config.dataset_path is None or rt.config.target_column is None:
        return None

    split_dir = rt.workspaces_root / "data_split"
    materialize_csv_split(
        rt.config.dataset_path,
        split_dir,
        rt.config.target_column,
        SplitSpec(
            search_frac=0.2,
            final_frac=0.2,
            seed=rt.config.split_seed,
            group_column=rt.config.group_column,
        ),
    )
    split = PlatformSplit(
        split_dir=split_dir,
        train_csv=split_dir / "train.csv",
        search_features_csv=split_dir / "search_features.csv",
        search_labels_csv=split_dir / "search_labels.csv",
        final_features_csv=split_dir / "final_features.csv",
        final_labels_csv=split_dir / "final_labels.csv",
        dataset_path=rt.config.dataset_path,
        target_column=rt.config.target_column,
        group_column=rt.config.group_column,
    )
    await rt.publish_output(
        source="supervisor",
        channel="text",
        text=(
            f"PREPARE: 平台已生成数据划分 {split_dir.resolve()}。"
            + (
                f"（按 {rt.config.group_column} 分组，同组不跨 split）"
                if rt.config.group_column
                else ""
            )
        ),
    )
    data_contract = split.data_contract()
    # 候选看不到任务文本（PlanInput 走 context_refs 死信道），所以这条约束必须
    # 单独持久化，再由 Supervisor 每一轮拼进 content（见 data_contract_block）。
    rt.state.data_contract = data_contract.contract_text()
    rt.state.save(rt.state_path)
    return data_contract


async def _prepare_evaluators(
    runtime: Any, evaluator_task: str
) -> tuple[Any, Any, Path, Path]:
    """Freeze the SEARCH evaluator and the hidden FINAL evaluator."""
    rt = runtime

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
            source="supervisor", channel="text", text="PREPARE: 生成评估器…"
        )
        evaluator_ref = await _run_evaluator_agent(
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
            text=f"PREPARE: evaluator 目录已接收 {evaluator_dir.resolve()}。",
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
            text="PREPARE: 生成 final evaluator…",
        )
        final_evaluator_ref = await _run_evaluator_agent(
            rt,
            directory_name="final_evaluator",
            agent_id=FINAL_EVALUATOR_AGENT_ID,
            plan_id=FINAL_EVALUATOR_PLAN_ID,
            task=(
                f"{evaluator_task}\n\nYou are building the FINAL evaluator. "
                "Use a held-out split disjoint from the SEARCH evaluator's "
                "split. This evaluator is hidden from SEARCH and used only "
                "by VALIDATE.\n\n"
                # 真机（2026-08-29）：不点名目录时，agent 用 shell_command 的绝对路径
                # 跑去改**已冻结的 SEARCH evaluator**，把它的 labels.csv 换成了 final
                # split 的标签——留出集就此泄漏——而自己的工作区一个文件都没有，于是
                # 连交 10 次 submit 全被拒，直到轮次预算耗尽。
                f"Your workspace is {final_evaluator_dir.resolve()} and it starts "
                "EMPTY. Every file you create — metric.json, evaluate.py, labels.csv, "
                "HANDOFF.md, pyproject.toml — must be written INSIDE it.\n"
                "The SEARCH evaluator directory already exists next to yours. It is "
                "frozen. Do NOT read from it, copy from it, or write into it, with "
                "either the file tools or shell_command. Building the final labels by "
                "editing the search evaluator's labels.csv leaks the held-out split "
                "into SEARCH and invalidates the whole run.\n"
                "Take the final labels from final_labels.csv in the platform's "
                "data_split directory, and write your own copy into your workspace."
            ),
            label="final_evaluator",
        )
        _assert_evaluator_splits_are_disjoint(
            evaluator_dir / "labels.csv", final_evaluator_dir / "labels.csv"
        )
        await rt.supervisor.checkpoint_final_evaluator(final_evaluator_ref)
        await rt.publish_output(
            source="supervisor",
            channel="text",
            text=(
                "PREPARE: final evaluator 目录已接收 "
                f"{final_evaluator_dir.resolve()}。"
            ),
        )

    return evaluator_ref, final_evaluator_ref, evaluator_dir, final_evaluator_dir


async def _prepare_eda(
    runtime: Any, workspace: Any, handoff_agent: HandoffFn
) -> bool:
    """Run the EDA orchestration; falls back to placeholder files on failure."""
    rt = runtime
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
        await handoff_agent(
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
        eda_root = Path(workspace.path)
        if failed:
            # 缺哪份补哪份的占位符，但不因此判定整个 EDA 失败：把 eda_ok 打成
            # False 会连锁跳过 EDA_INDEX/EDA_HANDOFF 汇总和 BASELINE_DESIGN，
            # 让盘上写好的其余报告一份都没人读。2026-08-29 与 2026-08-30 两轮
            # 都栽在这里：前者是汇总环节死掉，后者是单份报告被输出上限截断。
            _write_missing_report_placeholders(eda_root)
            await rt.publish_output(
                source="supervisor",
                channel="error",
                text=(
                    f"EDA todo failed: {failed}；已写占位符，"
                    f"用其余 {len(_usable_eda_reports(eda_root))} 份报告继续。"
                ),
            )
        if not _usable_eda_reports(eda_root):
            await rt.publish_output(
                source="supervisor",
                channel="error",
                text="EDA 无一份可用报告；降级为任务原文。",
            )
            _write_fallback_eda(eda_root)
            eda_ok = False
        else:
            await rt.publish_output(
                source="supervisor",
                channel="text",
                text="PREPARE: 汇总 EDA 报告…",
            )
            await handoff_agent(
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
            text=(
                f"EDA handoff failed ({error}); writing fallback EDA files.\n\n"
                f"{traceback.format_exc()}"
            ),
        )
        _write_fallback_eda(Path(workspace.path))
        eda_ok = False
        try:
            await rt.agents.reap(PREPARE_EDA_AGENT_ID)
        except Exception:  # noqa: BLE001,S110 - GC must never mask EDA failure
            pass
    return eda_ok


async def _prepare_baseline_design(
    runtime: Any,
    workspace: Any,
    candidate_task: str,
    eda_ok: bool,
    handoff_agent: HandoffFn,
) -> None:
    """Have the baseline ideator read EDA and write BASELINE_DESIGN.md."""
    rt = runtime
    if not eda_ok:
        await rt.publish_output(
            source="supervisor",
            channel="text",
            text="PREPARE: 因 EDA 失败跳过 BASELINE_DESIGN，prepare 将基于任务原文降级。",
        )
        return
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
        await handoff_agent(
            agent_id=BASELINE_IDEATOR_PROFILE.agent_type,
            agent_type=BASELINE_IDEATOR_PROFILE.agent_type,
            workspace=str(workspace.path),
            output_file="BASELINE_DESIGN.md",
            content=(
                f"{candidate_task}\n\nRead EDA_HANDOFF.md and write "
                "BASELINE_DESIGN.md."
            ),
            reap_after=True,
        )
    except Exception as error:
        await rt.publish_output(
            source="supervisor",
            channel="error",
            text=(
                f"Baseline design failed ({error}); prepare falls back to task-only.\n\n"
                f"{traceback.format_exc()}"
            ),
        )


async def _run_prepare_agent(
    runtime: Any,
    workspace: Any,
    evaluator_ref: Any,
    candidate_task: str,
    predict_features: Path | None,
) -> PrepareResult:
    """Run the PREPARE agent implementing the baseline and return trusted score."""
    rt = runtime
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
        task=candidate_task,
        max_turns=MAX_PLAN_TURNS,
        publish=lambda kind, ref, data: rt.events.project_agent_event(
            "prepare", kind, ref, data
        ),
        predict_features=predict_features,
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

    workspace = await _prepare_workspace(rt)
    data_contract = await _prepare_platform_split(rt)
    evaluator_task = (
        data_contract.evaluator_task(rt.task_text)
        if data_contract is not None
        else rt.task_text
    )
    candidate_task = (
        data_contract.candidate_task(rt.task_text)
        if data_contract is not None
        else rt.task_text
    )
    evaluator_ref, _final_evaluator_ref, _evaluator_dir, _final_evaluator_dir = (
        await _prepare_evaluators(rt, evaluator_task)
    )
    eda_ok = await _prepare_eda(rt, workspace, run_handoff_agent)
    await _prepare_baseline_design(rt, workspace, candidate_task, eda_ok, run_handoff_agent)
    return await _run_prepare_agent(
        rt,
        workspace,
        evaluator_ref,
        candidate_task,
        data_contract.predict_features_csv if data_contract is not None else None,
    )
