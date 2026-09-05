"""Command-line adapter for Athena's output/state-only research runtime."""

import argparse
import asyncio
import json
import sys
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from athena.core.agent import settings
from athena.core.project_lock import ProjectBusyError, project_lock
from athena.execution.check import check_compute, print_compute_check
from athena.execution.compute_config import (
    ComputeConfig,
    ComputeConfigError,
    load_compute_config,
)
from athena.kaggle import KaggleRunRequest, build_kaggle_stack, run_kaggle
from athena.research import ResearchRuntime
from athena.research.fork import ForkError, fork_project
from athena.research.literature.bench import (
    DEFAULT_QUERY_SET,
    DEFAULT_TOP_K,
    RELEVANT_THRESHOLD,
    corpus_health,
    delivery_overlap,
    dump_report,
    evaluate_recall,
    load_query_set,
    load_recall_set,
    run_known_item,
)
from athena.research.literature.bench import available as bench_available
from athena.research.literature.paper_rag.search import corpus_paper_ids
from athena.research.literature.paper_scout.schemas import RETAIN_THRESHOLD, ScoutCorpus
from athena.research.literature.survey import (
    SurveyRequest,
    build_survey_stack,
    run_survey,
)
from athena.research.literature.survey.report import print_check, print_report
from athena.research.runtime import DEFAULT_SURVEY_PAPERS
from athena.research.supervisor.plans import DEFAULT_EXPERIMENT_TIMEOUT_S


def _runtime(project_root: str, **options: Any) -> ResearchRuntime:
    """Build the public research composition root for one project.

    CLI is the non-interactive headless path. It explicitly uses the legacy
    compatibility policy: confirmation gate off and auto-confirm on. No implicit
    auto-confirm default exists at the library/runtime level.
    """
    return ResearchRuntime(
        project_root=Path(project_root),
        model=settings.model_name(),
        task_confirmation_gate=False,
        auto_confirm=True,
        **options,
    )


def _non_negative_int(value: str) -> int:
    """argparse ``type``：把 ``--max-search-experiments`` 约束为非负整数。

    负值在解析期就失败（exit 2），而非把 -1 之类一路传进 runtime 造成怪异行为。
    """
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative integer, got {value}")
    return parsed


def _non_negative_float(value: str) -> float:
    """argparse ``type``：把 ``--tolerance`` 约束为非负浮点数。

    ``PlanInput.tolerance`` 声明了 ``ge=0``，负值要到构造 Plan 时才炸，那时已经
    烧掉了一整轮 PREPARE。
    """
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative number, got {value}")
    return parsed


def _platform_split_dataset(args: argparse.Namespace) -> Path | None:
    """Return the CSV the platform should split itself, or None.

    ``--data`` is free-form: a CSV, a directory of images, a Kaggle URL. Only a
    local CSV with a named target can be split by ``materialize_csv_split``;
    handing it anything else raises inside PREPARE. So the platform-owned split
    turns on exactly when the arguments describe one, and stays off otherwise —
    which is the historical behaviour for every other shape of input.
    """
    if not args.target:
        return None
    path = Path(args.data)
    if path.suffix.lower() != ".csv" or not path.is_file():
        return None
    return path.resolve()


def _dataset_path_for_prompt(data: str) -> str:
    """Absolutize a local ``--data`` path before it goes into the task text.

    ``--data`` is resolved against the CLI's working directory, but every agent
    runs with its own workspace (or the project root) as cwd. A relative path
    therefore means two different things at the two ends, and the agent's end is
    the one that is wrong.

    Real run (2026-08-29): ``--data ../data/TESS-SF/windows/model_input.csv``
    passed the CLI's existence pre-check, then the first agent spent its whole
    turn budget hunting for it -- ``../data/...`` from the project root resolves
    to a sibling of the project, not of the CLI. Non-paths (a Kaggle URL) are
    passed through untouched.
    """
    path = Path(data)
    try:
        if path.exists():
            return str(path.resolve())
    except OSError:
        pass
    return data


@dataclass(frozen=True)
class CliRunConfig:
    """Typed translation of ``run`` CLI arguments passed to ``ResearchRuntime``.

    The CLI intentionally keeps this type internal: test-facing
    ``_runtime_options`` still returns the plain dict accepted by the runtime
    constructor, while the mapping stays one-directional here.
    """

    task: str
    search_limit: int | None
    auto_validate: bool
    direction: str
    ideation: str
    survey: bool
    survey_query: str
    survey_max_papers: int
    survey_search_top_k: int
    survey_max_seconds: float
    experiment_timeout_s: int
    compute: ComputeConfig
    dataset_path: Path | None
    target_column: str | None
    group_column: str | None
    split_seed: int
    tolerance: float
    data_root: str | None

    def as_options(self) -> dict[str, object]:
        return {
            "task": self.task,
            "search_limit": self.search_limit,
            "auto_validate": self.auto_validate,
            "direction": self.direction,
            "ideation": self.ideation,
            "survey": self.survey,
            "survey_query": self.survey_query,
            "survey_max_papers": self.survey_max_papers,
            "survey_search_top_k": self.survey_search_top_k,
            "survey_max_seconds": self.survey_max_seconds,
            "experiment_timeout_s": self.experiment_timeout_s,
            "compute": self.compute,
            "dataset_path": self.dataset_path,
            "target_column": self.target_column,
            "group_column": self.group_column,
            "split_seed": self.split_seed,
            "tolerance": self.tolerance,
            "data_root": self.data_root,
        }


def _runtime_options(args: argparse.Namespace) -> dict[str, object]:
    """Translate run arguments into supported ``ResearchRuntime`` options."""
    task_lines = [
        args.task or "",
        f"Dataset path: {_dataset_path_for_prompt(args.data)}",
    ]
    optional_context = (
        ("Target", args.target),
        ("Group column (rows sharing it must not span splits)", args.group_column),
        ("Task type", args.task_type),
        ("Data type", args.data_type),
        ("Primary metric", args.metric),
        ("K-fold policy", args.kfold if args.kfold != "auto" else None),
    )
    task_lines.extend(f"{label}: {value}" for label, value in optional_context if value)
    dataset_path = _platform_split_dataset(args)
    return CliRunConfig(
        task="\n".join(line for line in task_lines if line),
        # 传 None 而不是替换成默认值：运行时要能分辨"用户没给"和"用户给了 10"。
        # 给了就该覆盖持久化的旧值，没给就该沿用。
        search_limit=args.max_search_experiments,
        auto_validate=args.mode == "auto",
        direction=args.direction or "maximize",
        ideation=args.ideation,
        survey=args.survey,
        survey_query=args.survey_query or "",
        survey_max_papers=args.survey_papers,
        survey_search_top_k=args.survey_search_top_k,
        survey_max_seconds=args.survey_max_seconds,
        experiment_timeout_s=args.experiment_timeout,
        compute=_compute_config(args),
        # 这五项此前只被拼进任务提示词，从没传给运行时：平台数据划分因此永远
        # 不触发（``prepare_phase`` 要求 dataset_path 与 target_column 同时非空），
        # 判胜容差也永远是 0.0。
        dataset_path=dataset_path,
        target_column=args.target if dataset_path is not None else None,
        group_column=args.group_column if dataset_path is not None else None,
        split_seed=args.split_seed,
        tolerance=args.tolerance,
        data_root=args.data_root or None,
    ).as_options()


def _compute_config(args: argparse.Namespace) -> ComputeConfig:
    """把 ``--compute`` 覆盖叠到 ``config.toml`` 的 ``[compute]`` 上。"""
    config = load_compute_config()
    if args.compute is None or args.compute == config.mode:
        return config
    if args.compute == "ssh" and not config.hosts:
        raise ComputeConfigError("--compute ssh needs [[compute.hosts]] in config.toml")
    return replace(config, mode=args.compute)


class _EventRenderer:
    """Render runtime events to stdout, coalescing agent text fragments.

    The Supervisor streams agent text as many tiny ``output`` events (token by
    token); printing each floods stdout with fragments (``agent> on.``). Buffer
    ``source == "agent"`` text and flush one coherent ``agent> …`` line when the
    plan changes or a non-agent event arrives (mirrors ``scripts/run_headless.py``).
    """

    def __init__(self) -> None:
        self._agent_buf: list[str] = []
        self._agent_plan: object | None = None

    def _flush_agent(self) -> None:
        if not self._agent_buf:
            return
        head = "".join(self._agent_buf)
        print(
            f"agent> {head[:400]}..." if len(head) > 400 else f"agent> {head}",
            flush=True,
        )
        self._agent_buf = []
        self._agent_plan = None

    def render(self, kind: str, payload: dict[str, object]) -> None:
        if kind == "output":
            source = payload.get("source", "runtime")
            channel = payload.get("channel", "text")
            text = payload.get("text", "")
            if source == "agent" and channel == "text":
                plan = payload.get("plan")
                if self._agent_buf and plan != self._agent_plan:
                    self._flush_agent()
                self._agent_plan = plan
                if text:
                    self._agent_buf.append(text)
                return
            self._flush_agent()
            if text:
                print(f"{source}> {text}", flush=True)
            return
        if kind == "state":
            self._flush_agent()
            print(
                f"phase={payload.get('phase', '-')} "
                f"status={payload.get('status', '-')}",
                flush=True,
            )
            return
        raise ValueError(f"unsupported runtime event kind: {kind}")


def _apply_fork(args: argparse.Namespace) -> int:
    """按 ``--fork-from`` 把源项目的 PREPARE 产物复制到本次运行的目录。

    分叉失败直接返回非零退出码：让运行"降级成自己重跑一遍 PREPARE"会静默毁掉 A/B 的
    前提，而那种毁法在分数出来之前看不出来。
    """
    if not args.fork_from:
        return 0
    source = Path(args.fork_from).resolve()
    target = Path(args.project).resolve()
    try:
        result = fork_project(source, target)
    except ForkError as error:
        print(f"分叉失败：{error}", file=sys.stderr)
        return 2
    print(f"已从 {source} 分叉到 {target}")
    print(f"  共用 evaluator: {result.evaluator_ref}")
    print(f"  共用基线实验: {result.baseline_experiment_id}")
    print(f"  复制目录: {', '.join(result.copied) or '（无）'}")
    print("  语料已清空，本臂将自行决定是否调研")
    return 0


async def _cmd_run(args: argparse.Namespace) -> int:
    # 数据路径预检：拼错/缺失的 --data 应在跑 LLM 之前立刻失败，而非白烧一轮。
    if not Path(args.data).exists():
        print(f"error: data path does not exist: {args.data}", file=sys.stderr)
        return 2
    # 分叉必须在构造 runtime 之前：runtime 会读 .athena 里的 state 与 tree 决定起始阶段。
    forked = _apply_fork(args)
    if forked:
        return forked
    # 一个项目同时只允许一个 run。两个进程写同一份 state.json / research_tree.json
    # 时是最后写入者获胜，已结算的实验会静默消失（真机 2026-08-30：一个 SOTA 就这
    # 样从树里没了，两个进程都还在正常跑，唯一的迹象是树变短了）。
    try:
        with project_lock(Path(args.project) / ".athena"):
            return await _run_locked(args)
    except ProjectBusyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


async def _run_locked(args: argparse.Namespace) -> int:
    """Run one research session; the caller holds the project lock."""
    runtime = _runtime(args.project, **_runtime_options(args))
    terminal = asyncio.Event()
    exit_code = 0
    renderer = _EventRenderer()

    def receive(kind: str, payload: dict[str, object]) -> None:
        """先判终态再渲染：控制流不能挂在"打印成功"这个前提上。

        真实跑测（2026-08-16）：Agent 输出里一个 ✅ 让 ``print`` 在 GBK 控制台上抛
        ``UnicodeEncodeError``，异常在 ``terminal.set()`` 之前就把 ``receive`` 打断，
        于是一次已经 FAILED 的运行继续挂到 ``--timeout`` 才退出（实测挂了 25 分钟仍
        在跑）。渲染是尽力而为的旁支，失败只该少一行日志。
        """
        nonlocal exit_code
        if kind == "state":
            status = payload.get("status")
            phase = payload.get("phase")
            if status == "STOPPED" or phase == "COMPLETED":
                terminal.set()
            elif status == "FAILED":
                exit_code = 1
                terminal.set()
        try:
            renderer.render(kind, payload)
        except (UnicodeEncodeError, ValueError):
            # UnicodeEncodeError：终端编码装不下某个字符；ValueError：未知事件种类。
            print(f"[unrenderable {kind} event]", file=sys.stderr, flush=True)

    subscription_id = runtime.subscribe(receive)
    try:
        await runtime.start()
        # 无界等待在 CI/批处理下会卡死；--timeout 让运行时长可被限定。
        await asyncio.wait_for(terminal.wait(), timeout=args.timeout)
        return exit_code
    except TimeoutError:
        print(f"run timed out after {args.timeout}s", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI converts run failures to exit codes.
        # 启动或执行失败（缺 API key、git 初始化失败、模型连接失败等）：
        # 打印完整 traceback，返回非零退出码供脚本判失败。
        print(f"RUN FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        runtime.unsubscribe(subscription_id)
        await runtime.aclose()


async def _cmd_compute(args: argparse.Namespace) -> int:
    """连上 ``[compute]`` 里的每一台机器，把它实际长什么样打出来。"""
    config = _compute_config(args)
    dataset_root = Path(args.data_root).resolve() if args.data_root else None
    check = await check_compute(config, dataset_root=dataset_root)
    return print_compute_check(check)


async def _cmd_status(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project)
    snapshot: dict[str, object] = {}

    def receive(kind: str, payload: dict[str, object]) -> None:
        if kind == "state":
            snapshot.update(payload)

    subscription_id = runtime.subscribe(receive)
    try:
        print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        runtime.unsubscribe(subscription_id)
        await runtime.aclose()


async def _cmd_control(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project)
    command = "resume" if args.command == "continue" else args.command
    try:
        status = await runtime.message(f"/{command}")
        print(f"status={status}")
        return 0
    finally:
        await runtime.aclose()


async def _cmd_survey(args: argparse.Namespace) -> int:
    """跑一次 Academic Survey 全链路，打印成本账。

    ``--check`` 只装配依赖并报告可用能力，不发起任何检索；一篇都没转换成功时返回
    非零退出码，让批量运行能直接按退出码判断这次跑空了。
    """
    stack = build_survey_stack(
        artifact_root=args.artifact_root,
        model=args.model,
        scorer_model=args.scorer_model,
        enable_library=not args.no_library,
        library_root_path=args.library_root,
    )
    if args.check:
        return print_check(stack)
    arxiv_ids = [item.strip() for item in args.papers.split(",") if item.strip()]
    if not args.query.strip() and not arxiv_ids:
        print("需要 --query 或 --papers，或用 --check 只做装配自检。", file=sys.stderr)
        return 2
    report = await run_survey(
        stack,
        SurveyRequest(
            query=args.query or "direct fetch",
            arxiv_ids=arxiv_ids,
            max_papers=args.max_papers,
            max_steps=args.max_steps,
            max_seconds=args.max_seconds,
            search_top_k=args.search_top_k,
            expand_top_k=args.expand_top_k,
            retain_threshold=args.retain_threshold,
            require_retrievable_source=not args.allow_unfetchable,
            published_to=args.published_to,
            prefer=args.prefer,
            visual_policy=args.visual_policy,
            conversion_concurrency=args.concurrency,
            source_candidate_multiple=args.source_candidates,
            strict_quality=args.strict_quality,
            build_index=not args.no_index,
            fresh_scout=args.fresh_scout,
        ),
    )
    print_report(report)
    _write_report(report, args.out)
    return 0 if report.converted() else 1


def _print_kaggle_check(stack) -> int:
    print("Kaggle 装配自检")
    print(f"  下载根目录: {stack.download_root}")
    print(f"  凭据: {'已配置' if stack.client.configured else '未配置'}")
    return 0


def _print_kaggle_report(report) -> None:
    competition = report.competition
    print(f"\n竞赛：{report.competition_ref}")
    print(f"  状态: {report.status}")
    if competition is not None:
        print(f"  标题: {competition.title}")
        print(f"  评估指标: {competition.evaluation_metric or '（未给出）'}")
        print(f"  截止: {competition.deadline or '（未给出）'}")
    print(f"  下载文件: {len(report.downloaded_files)} 个")
    print(f"  notebook 证据: {len(report.notebooks)} 条")
    print(f"  耗时: {report.total_seconds}s  http={report.http_requests}")
    for warning in report.warnings:
        print(f"  - {warning}")


async def _cmd_kaggle(args: argparse.Namespace) -> int:
    """跑一次 Kaggle 竞赛取数，或只做装配自检 / 列出竞赛。

    ``--check`` 不发起任何网络请求；``--list`` 列出竞赛摘要；否则对
    ``--competition`` 走 connect → download → search notebooks 取数流程。
    """
    stack = build_kaggle_stack(download_root=args.download_dir)
    if args.check:
        return _print_kaggle_check(stack)
    if args.list:
        raw = await stack.client.list_competitions(
            search=args.search, sort_by=args.sort_by
        )
        for item in raw:
            print(
                f"{item.get('ref', '')}\t{item.get('title', '')}"
                f"\t{item.get('deadline', '')}\t{item.get('reward', '')}"
            )
        return 0
    if not args.competition.strip():
        print("需要 --competition，或用 --list / --check。", file=sys.stderr)
        return 2
    report = await run_kaggle(
        stack,
        KaggleRunRequest(
            competition=args.competition,
            download_subdir=args.download_subdir,
            max_notebooks=min(args.max_notebooks, 50),
        ),
    )
    _print_kaggle_report(report)
    _write_report(report, args.out)
    return 0 if report.status != "empty" else 1


def _write_report(report, path: str) -> None:
    """把报告落到 ``--out``；写不进去只报错，不抹掉已经跑完的那一轮。

    全链路要跑十几分钟并真的花钱，而报告此时已经打在屏幕上了。让一个打错的路径
    以 traceback 结束整条命令，丢的是唯一一份成本账，而不是那个路径。
    """
    if not path:
        return
    try:
        Path(path).write_text(
            json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as error:
        # 路径不存在/无权限/盘满 → 报告已打印，只提示写入失败
        print(f"\n报告写入失败（{path}）：{error}", file=sys.stderr)
        return
    print(f"\n报告已写入 {path}")


async def _dispatch_command(args: argparse.Namespace) -> int:
    if args.command == "run":
        return await _cmd_run(args)
    if args.command == "survey":
        return await _cmd_survey(args)
    if args.command == "bench":
        return await _cmd_bench(args)
    if args.command == "kaggle":
        return await _cmd_kaggle(args)
    if args.command == "compute":
        return await _cmd_compute(args)
    if args.command == "status":
        return await _cmd_status(args)
    return await _cmd_control(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="Athena-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--project", required=True, help="project root")
    run.add_argument("--data", required=True, help="input dataset path")
    run.add_argument("--task", help="research intent")
    run.add_argument("--target", help="supervised target column")
    run.add_argument(
        "--group-column",
        help=(
            "分组列名：同一取值的行绝不跨 train/search/final（活动区、恒星、受试者）。"
            "行与行不独立时，缺了它的随机划分会静默泄漏且分数只会更好看"
        ),
    )
    run.add_argument(
        "--split-seed",
        type=int,
        default=0,
        help="平台数据划分的随机种子（默认 0）",
    )
    run.add_argument(
        "--tolerance",
        type=_non_negative_float,
        default=0.0,
        help=(
            "判胜容差：候选须超过参考指标至少这么多才算 WIN（默认 0.0，"
            "即任意大于都算赢）"
        ),
    )
    run.add_argument(
        "--data-root",
        default="",
        help="数据集根目录；与远端算力一起用时用于分发",
    )
    run.add_argument("--task-type", help="task type, for example classification")
    run.add_argument("--data-type", help="data type, for example tabular")
    run.add_argument("--metric", help="primary evaluation metric")
    run.add_argument(
        "--direction", choices=["maximize", "minimize"], help="metric direction"
    )
    # CLI 是无交互输入的 headless 适配器；``interactive``（手动批准 VALIDATE）在
    # 这里没有 stdin 循环可驱动，会停在 WAITING 永久挂起。默认 ``auto`` 让一次跑完。
    run.add_argument("--mode", choices=["interactive", "auto"], default="auto")
    run.add_argument(
        "--kfold", choices=["required", "auto", "disabled"], default="auto"
    )
    run.add_argument(
        "--max-search-experiments",
        type=_non_negative_int,
        help="maximum SEARCH attempts",
    )
    run.add_argument(
        "--timeout",
        type=_non_negative_int,
        default=None,
        help="abort the run after N seconds (unbounded by default)",
    )
    run.add_argument(
        "--compute",
        choices=["local", "ssh"],
        default=None,
        help=(
            "算力：local 在本机跑，ssh 派到 config.toml 的 [[compute.hosts]] 上。"
            "不给则按 config.toml 里的 [compute].mode。绝不静默降级"
        ),
    )
    run.add_argument(
        "--experiment-timeout",
        type=int,
        default=DEFAULT_EXPERIMENT_TIMEOUT_S,
        help=(
            "单条 experiment.json 命令的超时上限（秒，默认 "
            f"{DEFAULT_EXPERIMENT_TIMEOUT_S}）"
        ),
    )
    run.add_argument(
        "--ideation",
        choices=["ideageneration", "baseline", "debate"],
        default="ideageneration",
        help=(
            "hypothesis intake: 'ideageneration' (default) runs the Idea Generation "
            "quality gate (structural + falsifiability checks, review perspectives) "
            "before hypotheses enter the tree; 'baseline' is the ablation control "
            "that registers Ideator output as-is; 'debate' uses the debate-based "
            "Ideator (proposal -> review -> revision -> judge)"
        ),
    )
    # 默认关：一次调研是十几分钟的模型往返，不能由默认值替用户决定花这笔钱。
    run.add_argument(
        "--survey",
        action="store_true",
        help=(
            "跑一次文献调研，让 Ideator 除数据集外还能读论文。与 PREPARE 并行，"
            "不占关键路径，但要花十几分钟的模型调用；默认关闭"
        ),
    )
    run.add_argument(
        "--survey-query",
        default="",
        help="文献检索式；留空则由研究任务提炼一句主题",
    )
    run.add_argument(
        "--survey-papers",
        type=_non_negative_int,
        default=DEFAULT_SURVEY_PAPERS,
        help="进入语料的论文篇数；成本大致随它线性增长",
    )
    run.add_argument(
        "--survey-search-top-k",
        type=_non_negative_int,
        default=0,
        help="loop 内调研的检索深度；0 表示用 SurveyRequest 的默认值",
    )
    run.add_argument(
        "--survey-max-seconds",
        type=float,
        default=0.0,
        help="loop 内调研的检索墙钟预算；0 表示用默认值。与 --survey-search-top-k 一起调",
    )
    run.add_argument(
        "--fork-from",
        default="",
        help=(
            "从一个已 PREPARE 完的项目分叉：共用它冻结的 evaluator 与基线 commit，"
            "直接从 SEARCH 起步。做 A/B 时两臂都从同一个源分叉，才只剩 ideation 一个"
            "变量——两臂各跑各的 PREPARE 时，基线方差与待测效应同量级"
        ),
    )
    _add_compute_parser(subparsers)
    _add_survey_parser(subparsers)
    _add_bench_parser(subparsers)
    _add_kaggle_parser(subparsers)
    for name in ("status", "pause", "resume", "continue", "stop"):
        subparsers.add_parser(name).add_argument(
            "--project", required=True, help="project root"
        )
    return parser


def _add_survey_parser(subparsers) -> None:
    """挂上 ``survey`` 子命令：一句主题 → 一个可检索的论文语料库。

    默认值全部来自 ``SurveyRequest``，这里只负责暴露；改默认值请改那边，别在两处
    各写一份。
    """
    defaults = SurveyRequest(query="_")
    survey = subparsers.add_parser("survey", help="build a paper corpus on a topic")
    survey.add_argument("--query", default="", help="调研主题")
    survey.add_argument(
        "--papers",
        default="",
        help="逗号分隔的 arXiv id，给了就跳过检索直接取源（用于单独测量下游各段）",
    )
    survey.add_argument(
        "--max-papers",
        type=int,
        default=defaults.max_papers,
        help="交给下游的篇数；只影响取源/转换/索引，检索成本由 --max-steps 决定",
    )
    survey.add_argument(
        "--max-steps", type=int, default=defaults.max_steps, help="PaperScout 步数上限"
    )
    survey.add_argument(
        "--max-seconds",
        type=float,
        default=defaults.max_seconds,
        help=(
            "检索墙钟预算。它一直是真正绑定的那条约束（真机多轮 stop_reason 都是 "
            "max_seconds、停在第 4 步），所以调 --search-top-k 时必须一起放宽"
        ),
    )
    survey.add_argument(
        "--search-top-k",
        type=int,
        default=defaults.search_top_k,
        help=(
            "每次 search 取回几条。论文的 10 是给稠密语义索引设的，换成词法排序后不成立"
            "——实测同一批查询在深度 10 只浮现 1/10 篇 gold，深度 50 浮现 6/10，100 无增益"
        ),
    )
    survey.add_argument(
        "--expand-top-k",
        type=int,
        default=defaults.expand_top_k,
        help="每次 expand 沿参考文献取回几条",
    )
    survey.add_argument(
        "--retain-threshold",
        type=float,
        default=defaults.retain_threshold,
        help=(
            f"交付门槛，默认 {RETAIN_THRESHOLD}（整池按相关性排序，由 --max-papers 截断）。"
            "打分离散，非零门槛只有三档：>0.45 只要 3 分，>0.2 含 2 分，>0 含 1 分。"
            "只有 3 分档跨模型稳定，换 --scorer-model 后别用 0.3"
        ),
    )
    survey.add_argument(
        "--allow-unfetchable",
        action="store_true",
        help=(
            "让取不到源的论文也参与交付（默认剔除：既无 arXiv id、上游也没给开放获取"
            "链接的论文下载不到，却会占掉一个交付名额）"
        ),
    )
    survey.add_argument(
        "--fresh-scout",
        action="store_true",
        help=(
            "即使论文库里已有同一份检索请求，也重新跑一遍 PaperScout。默认复用："
            "同一份 ScoutRequest 是确定性输入，重跑只是把 71% 的墙钟再付一遍"
        ),
    )
    survey.add_argument(
        "--no-library",
        action="store_true",
        help=(
            "关掉论文库，每篇都重新下载、重新转换、重新编码。只在核对"
            "缓存是否掩盖了问题时才需要"
        ),
    )
    survey.add_argument(
        "--library-root",
        default="",
        help="论文库根目录；默认 ATHENA_LIBRARY_ROOT 或 ~/.athena/library",
    )
    survey.add_argument("--published-to", default="", help="发布日期上限 ISO")
    survey.add_argument(
        "--prefer",
        default=defaults.prefer,
        choices=["tex", "pdf", "both"],
        help="取源偏好",
    )
    survey.add_argument(
        "--visual-policy",
        default=defaults.visual_policy,
        choices=["best_effort", "required"],
        help="视觉解读严格度",
    )
    survey.add_argument(
        "--concurrency",
        type=int,
        default=defaults.conversion_concurrency,
        help="并发转换篇数",
    )
    survey.add_argument(
        "--source-candidates",
        type=int,
        default=defaults.source_candidate_multiple,
        help=(
            "交给取源的候选是 --max-papers 的几倍；取源顺序尝试、够数即停，"
            "多余的候选不会被下载"
        ),
    )
    survey.add_argument(
        "--strict-quality",
        action="store_true",
        help="只把 pass / pass_with_notes 的论文放进语料（默认放行 degraded）",
    )
    survey.add_argument("--no-index", action="store_true", help="跳过建索引")
    survey.add_argument("--artifact-root", default="", help="artifact 根目录")
    survey.add_argument("--model", default="", help="策略模型")
    survey.add_argument(
        "--scorer-model",
        default="",
        help="相关性打分模型；留空沿用策略模型。打分是调用最多的一环，换轻量模型最省时间",
    )
    survey.add_argument("--out", default="", help="把 SurveyReport JSON 写到该路径")
    survey.add_argument("--check", action="store_true", help="只做装配自检")


def _add_bench_parser(subparsers) -> None:
    """挂上 ``bench`` 子命令：把此前一次性脚本做的测量变成能重跑、能 diff 的东西。

    两个子模式对应两类问题：``retrieval`` 问"改写过的提问能不能找回那篇论文"，
    ``health`` 问"这份语料的结构够不够用"。后者不需要任何模型，因此在没有编码器的
    环境里也能跑。
    """
    bench = subparsers.add_parser(
        "bench", help="measure retrieval quality and corpus health on a built corpus"
    )
    modes = bench.add_subparsers(dest="bench_command", required=True)

    overlap = modes.add_parser(
        "overlap", help="how reproducible two surveys' delivered paper sets are"
    )
    overlap.add_argument(
        "--corpus",
        action="append",
        required=True,
        metavar="REF",
        help=(
            "corpus_ref of one run; pass twice or more. Below ~0.3 mean Jaccard the "
            "corpus is effectively a fresh random draw each run and no A/B over it "
            "can mean much"
        ),
    )
    overlap.add_argument("--query", default="", help="调研主题，仅写进报告")

    recall = modes.add_parser(
        "recall", help="split the loss into not-found / not-judged / not-delivered"
    )
    recall.add_argument(
        "--pool",
        required=True,
        help="ScoutCorpus 的 artifact 引用（一次检索跑出来的池子与交付集合）",
    )
    recall.add_argument(
        "--gold",
        default="imbalance_auc_recall",
        help="随包召回金标的名字，或一份 RecallQuerySet JSON 的路径",
    )
    recall.add_argument(
        "--threshold",
        type=float,
        default=RELEVANT_THRESHOLD,
        help=f"判为相关的分数线，默认 {RELEVANT_THRESHOLD}（2 分档）",
    )

    retrieval = modes.add_parser(
        "retrieval", help="known-item hit@k and MRR per channel"
    )
    retrieval.add_argument("--corpus", required=True, help="corpus_ref to benchmark")
    retrieval.add_argument(
        "--queries",
        default=DEFAULT_QUERY_SET,
        help=(
            f"packaged query set name ({', '.join(bench_available())}) or a path to a "
            "QuerySet JSON file"
        ),
    )
    retrieval.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="返回条数")
    retrieval.add_argument(
        "--no-semantic",
        action="store_true",
        help="只跑词面通道，不调编码 API（离线核对关键词检索的回归时用）",
    )

    health = modes.add_parser("health", help="structural invariants of a corpus")
    health.add_argument("--corpus", required=True, help="corpus_ref to inspect")
    health.add_argument(
        "--detail", action="store_true", help="逐篇列出，而不是只给汇总"
    )

    for parser in (retrieval, health, overlap, recall):
        parser.add_argument(
            "--artifact-root",
            default="",
            help="artifact 根目录；默认 ~/.athena/artifacts",
        )
        parser.add_argument(
            "--out", default="", help="把报告写成 JSON，供两次运行 diff"
        )


def _print_retrieval_report(report) -> None:
    """打印逐通道成绩；未命中的查询单独列出来，因为它们才是要改的东西。"""
    print(f"\nknown-item 基准：{report.query_set}")
    print(f"  语料: {report.corpus_papers} 篇 / {report.corpus_chunks} chunk")
    print(f"  编码器: {report.embedding_model or '（无）'}  k={report.top_k}")
    if report.unusable_queries:
        print(
            f"  金标不在本语料、已排除: {', '.join(report.unusable_queries)}"
            "  （这不是检索失败）"
        )
    header = (
        f"  {'通道':<26}{'hit@1':>7}{'hit@3':>7}{'hit@5':>7}{'未命中':>8}{'MRR':>8}"
    )
    print(header)
    for channel in report.channels:
        print(
            f"  {channel.channel:<26}"
            f"{channel.hit_at_1:>4}/{channel.scored:<2}"
            f"{channel.hit_at_3:>4}/{channel.scored:<2}"
            f"{channel.hit_at_5:>4}/{channel.scored:<2}"
            f"{channel.missed:>8}{channel.mrr:>8.3f}"
        )
    for channel in report.channels:
        missed = [item.query_id for item in channel.outcomes if item.rank is None]
        if missed:
            print(f"  {channel.channel} 未命中: {', '.join(missed)}")


def _print_health_report(report, detail: bool) -> None:
    """打印结构体检；每一行都对应一条"够不够用"的判据。"""
    print(f"\n语料体检：{report.corpus_ref}")
    print(f"  规模: {report.papers} 篇 / {report.chunks} chunk / {report.sentences} 句")
    print(
        f"  语义检索: {'可用' if report.semantic_search else '不可用'}"
        f"  编码器: {report.embedding_model or '（无）'}"
    )
    print(
        f"  摘要覆盖: {report.abstract_coverage:.0%}"
        f"  门面几乎无正文的篇数: {report.thin_anchor_papers}"
    )
    print(
        f"  语料内引用边: {report.citation_edges}"
        f"（每篇 {report.citation_density:.2f}）  图文互链: {report.visual_links}"
    )
    print("  章节覆盖（篇数）:")
    for name, count in report.section_coverage.items():
        print(f"    {name:<14}{count:>4} / {report.papers}")
    if not detail:
        return
    print("  逐篇:")
    for item in report.papers_detail:
        flags = []
        if not item.has_abstract_chunk:
            flags.append(f"无摘要chunk(锚点={item.anchor_kind})")
        if item.anchor_prose_chars < 40:
            flags.append(f"门面正文{item.anchor_prose_chars}字")
        print(
            f"    {item.paper_id:<38}{item.chunks:>5} chunk"
            f"{item.outbound_citations:>4} 引用  {'; '.join(flags)}"
        )


def _print_recall_report(report) -> None:
    """打印三段召回；每一段丢的东西该动的地方不同，所以分开报。"""
    print(f"\n召回评测：{report.query_set}")
    print(f"  课题: {report.topic}")
    print(
        f"  金标 {report.gold_total} 篇 · 池子 {report.pool_size} 篇 · "
        f"交付 {report.delivered_size} 篇 · 相关线 {report.threshold}"
    )
    labels = {
        "in_pool": "进池（检索找到）",
        "judged_relevant": "判为相关（打分给够）",
        "delivered": "进交付（名额与取源）",
    }
    for stage in report.stages:
        print(
            f"  {labels[stage.stage]:<24}{stage.found:>4}/{report.gold_total}"
            f"  召回 {stage.recall:.3f}   本段丢 {stage.lost_here}"
        )
    print(f"\n  金标来源: {report.gold_source}")


def _print_overlap_report(report) -> None:
    """打印交付重合度；这是选片改动唯一的验收指标。"""
    subject = f"：{report.query}" if report.query else ""
    print(f"\n交付可复现性{subject}")
    print(f"  比较了 {report.runs} 次运行，各交付 {report.delivered} 篇")
    print(
        f"  平均两两 Jaccard: {report.mean_jaccard:.3f}"
        f"  最低: {report.min_jaccard:.3f}"
    )
    print(f"  稳定核心: {len(report.stable_core)} 篇  并集: {report.union_size} 篇")
    if report.mean_jaccard < 0.3:
        print(
            "  低于 0.3：每次跑出来的基本是一次新的随机抽样，"
            "在它之上做的任何 A/B 都说明不了问题"
        )
    for paper_id in report.stable_core:
        print(f"    {paper_id}")


async def _cmd_bench(args: argparse.Namespace) -> int:
    """跑一次基准并打印报告；``--out`` 时同时落一份可 diff 的 JSON。

    语义通道需要与建索引时**同一个**编码器，因此这里复用 ``build_survey_stack`` 而不是
    另建一个：换了模型的向量与索引里的向量不在同一个空间，得到的分数看上去正常、实际
    毫无意义——正是这条链路反复吃过的那种亏。
    """
    stack = build_survey_stack(
        artifact_root=args.artifact_root, enable_vision=False, enable_library=False
    )
    if args.bench_command == "recall":
        corpus = ScoutCorpus.model_validate_json(
            await stack.artifacts.get_text(args.pool)
        )
        report = evaluate_recall(
            load_recall_set(args.gold),
            {item.paper_key: item.relevance for item in corpus.pool},
            [item.paper_key for item in corpus.retained],
            threshold=args.threshold,
        )
        _print_recall_report(report)
        if args.out:
            print(f"\n报告已写入 {dump_report(report, args.out)}")
        return 0
    if args.bench_command == "overlap":
        runs = [
            sorted(
                corpus_paper_ids(await stack.corpus_cache.load(stack.artifacts, ref))
            )
            for ref in args.corpus
        ]
        report = delivery_overlap(runs, query=args.query, label="corpus delivery")
        _print_overlap_report(report)
        if args.out:
            print(f"\n报告已写入 {dump_report(report, args.out)}")
        return 0
    corpus = await stack.corpus_cache.load(
        stack.artifacts, args.corpus, vectors=args.bench_command == "retrieval"
    )
    if args.bench_command == "health":
        report = corpus_health(corpus, args.corpus)
        _print_health_report(report, args.detail)
    else:
        embedder = None if args.no_semantic else stack.embedder
        if embedder is None and not args.no_semantic:
            print("未配置 ATHENA_EMBEDDING_MODEL，只跑词面通道。", file=sys.stderr)
        report = await run_known_item(
            corpus,
            load_query_set(args.queries),
            corpus_ref=args.corpus,
            embedder=embedder,
            top_k=args.top_k,
        )
        _print_retrieval_report(report)
    if args.out:
        print(f"\n报告已写入 {dump_report(report, args.out)}")
    return 0


def _add_compute_parser(subparsers) -> None:
    """挂上 ``compute`` 子命令：算力自检。"""
    compute = subparsers.add_parser(
        "compute", help="check the GPU hosts in [compute] before running anything"
    )
    compute.add_argument(
        "--compute", choices=["local", "ssh"], help="临时覆盖 [compute].mode"
    )
    compute.add_argument(
        "--data-root", default="", help="数据集根目录；与远端算力一起用时用于分发"
    )


def _add_kaggle_parser(subparsers) -> None:
    """挂上 ``kaggle`` 子命令：连接竞赛 → 下载 → 检索 notebook，或列出竞赛 / 自检。"""
    kaggle = subparsers.add_parser("kaggle", help="fetch a Kaggle competition's data")
    kaggle.add_argument("--competition", default="", help="竞赛 slug，如 titanic")
    kaggle.add_argument(
        "--download-dir", default="", help="下载根目录，默认 cwd/kaggle-data"
    )
    kaggle.add_argument(
        "--download-subdir", default="", help="下载子目录，默认竞赛 slug"
    )
    kaggle.add_argument(
        "--max-notebooks",
        type=_non_negative_int,
        default=10,
        help="notebook 证据数（0-50）",
    )
    kaggle.add_argument("--list", action="store_true", help="列出竞赛而不是跑流水线")
    kaggle.add_argument("--search", default="", help="--list 时的标题过滤词")
    kaggle.add_argument("--sort-by", default="latestDeadline", help="--list 排序字段")
    kaggle.add_argument("--check", action="store_true", help="只做装配自检")
    kaggle.add_argument("--out", default="", help="把报告 JSON 写到该路径")


def _make_output_encodable() -> None:
    """把 stdout/stderr 切成 UTF-8，装不下的字符退化成替代符而不是抛异常。

    Windows 上重定向后的 stdout 默认是 GBK，Agent 正文里的 ✅/中文标点会让 ``print``
    直接抛 ``UnicodeEncodeError``。这里只影响本进程的输出编码，不改任何业务行为。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _make_output_encodable()
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_dispatch_command(args))


if __name__ == "__main__":
    raise SystemExit(main())
