"""Command-line adapter for Athena's output/state-only research runtime."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from athena.core.agent import settings
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
        "search_limit": args.max_search_experiments or 10,
        "auto_validate": args.mode == "auto",
        "direction": args.direction or "maximize",
        "survey": args.survey,
        "survey_query": args.survey_query,
        "survey_max_papers": args.survey_papers,
    }


def _render_event(kind: str, payload: dict[str, object]) -> None:
    if kind == "output":
        source = payload.get("source", "runtime")
        text = payload.get("text", "")
        if text:
            print(f"{source}> {text}", flush=True)
        return
    if kind == "state":
        print(
            f"phase={payload.get('phase', '-')} "
            f"status={payload.get('status', '-')}",
            flush=True,
        )
        return
    raise ValueError(f"unsupported runtime event kind: {kind}")


async def _cmd_run(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project, **_runtime_options(args))
    terminal = asyncio.Event()
    exit_code = 0

    def receive(kind: str, payload: dict[str, object]) -> None:
        nonlocal exit_code
        _render_event(kind, payload)
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
        await terminal.wait()
        return exit_code
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
    run.add_argument("--mode", choices=["interactive", "auto"], default="interactive")
    run.add_argument(
        "--kfold", choices=["required", "auto", "disabled"], default="auto"
    )
    run.add_argument(
        "--max-search-experiments", type=int, help="maximum SEARCH attempts"
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_dispatch_command(args))


if __name__ == "__main__":
    raise SystemExit(main())
