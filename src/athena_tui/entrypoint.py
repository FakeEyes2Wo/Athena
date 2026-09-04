"""Athena TUI argument parsing, runtime composition, and TTY checks."""

import argparse
import asyncio
import sys
from pathlib import Path

from athena.core.agent import settings
from athena.research import ResearchRuntime
from athena.research.dataset_contract import platform_split_dataset
from athena.research.supervisor.plans import DEFAULT_EXPERIMENT_TIMEOUT_S
from athena_tui.app import AthenaApp

_AUTOMATION_HINT = (
    "Athena TUI 需要交互式终端。自动化请使用 Athena-cli：\n"
    '  uv run Athena-cli run --project {project} --task "..." --data ...'
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return parsed


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="Athena-tui")
    parser.add_argument("--project", default=".athena/tui-run", help="项目根目录")
    parser.add_argument("--search-limit", type=_positive_int, default=None)
    parser.add_argument(
        "--validate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="SOTA 落定后自动进入 VALIDATE（默认开）",
    )
    # 平台数据契约。缺了这几项，PREPARE 不会做平台划分，而是退回行级随机切分：
    # 同一个分组（活动区、恒星、受试者）的行会跨 split，泄漏是静默的，分数只会更
    # 好看。CLI 一直有这些参数，TUI 没有，所以 TUI 起的跑批没法用于正式结果。
    parser.add_argument("--data", default=None, help="数据集路径；本地 CSV 才能由平台划分")
    parser.add_argument("--target", default=None, help="监督目标列名")
    parser.add_argument(
        "--group-column",
        default=None,
        help="分组列名：同一取值的行绝不跨 train/search/final（活动区、恒星、受试者）",
    )
    parser.add_argument("--split-seed", type=int, default=0, help="平台划分随机种子")
    parser.add_argument(
        "--data-root",
        default=None,
        help="数据集根目录，作为 ATHENA_DATA_ROOT 交给命令；表格之外的伴随文件放这里",
    )
    parser.add_argument(
        "--direction", choices=["maximize", "minimize"], default="maximize"
    )
    parser.add_argument(
        "--tolerance",
        type=_non_negative_float,
        default=0.0,
        help="判胜容差：候选须超过参考指标至少这么多才算 WIN",
    )
    parser.add_argument(
        "--experiment-timeout",
        type=_positive_int,
        default=DEFAULT_EXPERIMENT_TIMEOUT_S,
        help="单个实验命令的超时秒数",
    )
    parser.add_argument(
        "--survey",
        action="store_true",
        help="跑一次文献调研，让 Ideator 除数据集外还能读论文；与 PREPARE 并行",
    )
    parser.add_argument("--survey-query", default="", help="文献检索式；留空则由任务提炼")
    return parser.parse_args(argv)


def _is_interactive() -> bool:
    """Return whether stdin and stdout are both interactive terminals."""
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _dataset_options(args: argparse.Namespace) -> dict[str, object]:
    """Resolve the platform split contract, using the same rule as the CLI."""
    dataset_path = platform_split_dataset(args.data, args.target)
    return {
        "dataset_path": dataset_path,
        # 只有平台真的接管划分时才转发这两项：否则 PREPARE 会拿到一个它不会用的
        # 分组列，而调用方以为分组生效了。
        "target_column": args.target if dataset_path is not None else None,
        "group_column": args.group_column if dataset_path is not None else None,
        "split_seed": args.split_seed,
        "data_root": args.data_root,
        "direction": args.direction,
        "tolerance": args.tolerance,
        "experiment_timeout_s": args.experiment_timeout,
        "survey": args.survey,
        "survey_query": args.survey_query,
    }


def _describe_split(options: dict[str, object]) -> str:
    """One line stating whether the platform split is on, printed before the TUI.

    A silent fallback to a row-level shuffle is the failure this guards; it has
    to be visible at startup rather than inferred from a suspiciously good score
    hours later.
    """
    dataset_path = options["dataset_path"]
    if dataset_path is None:
        return (
            "平台划分：关闭（未给 --data/--target，或 --data 不是本地 CSV）。"
            "评估器将自行切分，分组不受保护。"
        )
    group = options["group_column"]
    grouping = f"按 {group} 分组" if group else "无分组列：行级随机划分，相关行会跨 split"
    return (
        f"平台划分：开启 · {dataset_path} · 目标列 {options['target_column']} · "
        f"{grouping} · seed {options['split_seed']}"
    )


async def _run(args: argparse.Namespace) -> int:
    options = _dataset_options(args)
    print(_describe_split(options), file=sys.stderr)
    runtime = ResearchRuntime(
        project_root=Path(args.project),
        model=settings.model_name(),
        auto_seed_task=True,
        auto_validate=args.validate,
        search_limit=args.search_limit,
        **options,
    )
    # Resume: a prior run with a trusted baseline/SOTA auto-recovers and
    # continues; otherwise the first Human message seeds the task.
    if runtime.tree.best_experiment_id() is not None:
        await runtime.start()
    app = AthenaApp(runtime, Path(args.project))
    return await app.run()


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the full-screen TUI."""
    args = _parse(sys.argv[1:] if argv is None else argv)
    if not _is_interactive():
        print(_AUTOMATION_HINT.format(project=args.project), file=sys.stderr)
        return 1
    return asyncio.run(_run(args))
