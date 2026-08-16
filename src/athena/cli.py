"""Command-line adapter for Athena's output/state-only research runtime."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from athena.core.agent import settings
from athena.kaggle import KaggleRunRequest, build_kaggle_stack, run_kaggle
from athena.research import ResearchRuntime
from athena.research.paper_scout.schemas import RETAIN_THRESHOLD
from athena.research.survey import (
    SurveyRequest,
    build_survey_stack,
    run_survey,
)
from athena.research.survey.report import print_check, print_report


def _runtime(project_root: str, **options: Any) -> ResearchRuntime:
    """Build the public research composition root for one project."""
    return ResearchRuntime(
        project_root=Path(project_root),
        model=settings.model_name(),
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


def _runtime_options(args: argparse.Namespace) -> dict[str, object]:
    """Translate run arguments into supported ``ResearchRuntime`` options."""
    task_lines = [args.task or "", f"Dataset path: {args.data}"]
    optional_context = (
        ("Target", args.target),
        ("Task type", args.task_type),
        ("Data type", args.data_type),
        ("Primary metric", args.metric),
        ("K-fold policy", args.kfold if args.kfold != "auto" else None),
    )
    task_lines.extend(f"{label}: {value}" for label, value in optional_context if value)
    return {
        "task": "\n".join(line for line in task_lines if line),
        "search_limit": (
            args.max_search_experiments
            if args.max_search_experiments is not None
            else 10
        ),
        "auto_validate": args.mode == "auto",
        "direction": args.direction or "maximize",
        "ideation": args.ideation,
        "survey": args.survey,
        "survey_query": args.survey_query,
        "survey_max_papers": args.survey_papers,
    }


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


async def _cmd_run(args: argparse.Namespace) -> int:
    # 数据路径预检：拼错/缺失的 --data 应在跑 LLM 之前立刻失败，而非白烧一轮。
    if not Path(args.data).exists():
        print(f"error: data path does not exist: {args.data}", file=sys.stderr)
        return 2
    runtime = _runtime(args.project, **_runtime_options(args))
    terminal = asyncio.Event()
    exit_code = 0
    renderer = _EventRenderer()

    def receive(kind: str, payload: dict[str, object]) -> None:
        nonlocal exit_code
        renderer.render(kind, payload)
        if kind != "state":
            return
        status = payload.get("status")
        phase = payload.get("phase")
        if status == "STOPPED" or phase == "COMPLETED":
            terminal.set()
        elif status == "FAILED":
            exit_code = 1
            terminal.set()

    subscription_id = runtime.subscribe(receive)
    try:
        await runtime.start()
        # 无界等待在 CI/批处理下会卡死；--timeout 让运行时长可被限定。
        await asyncio.wait_for(terminal.wait(), timeout=args.timeout)
        return exit_code
    except asyncio.TimeoutError:
        print(f"run timed out after {args.timeout}s", file=sys.stderr)
        return 1
    except Exception as exc:
        # 启动或执行失败（缺 API key、git 初始化失败、模型连接失败等）：
        # 打印一行干净错误而非裸 traceback，返回非零退出码供脚本判失败。
        print(f"RUN FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        runtime.unsubscribe(subscription_id)
        await runtime.aclose()


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
    try:
        status = await runtime.message(f"/{args.command}")
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
            retain_threshold=args.retain_threshold,
            require_retrievable_source=not args.allow_unfetchable,
            published_to=args.published_to,
            prefer=args.prefer,
            visual_policy=args.visual_policy,
            conversion_concurrency=args.concurrency,
            source_candidate_multiple=args.source_candidates,
            strict_quality=args.strict_quality,
            build_index=not args.no_index,
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
        stack.client,
        stack.artifacts,
        stack.download_root,
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
    if args.command == "kaggle":
        return await _cmd_kaggle(args)
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
        type=int,
        default=10,
        help="进入语料的论文篇数；成本大致随它线性增长",
    )
    _add_survey_parser(subparsers)
    _add_kaggle_parser(subparsers)
    for name in ("status", "pause", "resume", "stop"):
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
        "--max-seconds", type=float, default=defaults.max_seconds, help="检索墙钟预算"
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
    kaggle.add_argument(
        "--sort-by", default="latestDeadline", help="--list 排序字段"
    )
    kaggle.add_argument("--check", action="store_true", help="只做装配自检")
    kaggle.add_argument("--out", default="", help="把报告 JSON 写到该路径")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_dispatch_command(args))


if __name__ == "__main__":
    raise SystemExit(main())
