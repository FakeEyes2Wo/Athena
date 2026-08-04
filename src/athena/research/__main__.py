"""Academic Survey 全链路入口：``uv run python -m athena.research --query "..."``。

``--check`` 只装配依赖并报告可用能力，不发起任何检索；``--dry-run`` 走到取源请求
为止就停下，用来在花钱下载之前确认 scout 的交付集合是不是想要的东西。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from athena.research.paper_scout.schemas import RETAIN_THRESHOLD
from athena.research.pipeline import SurveyReport, SurveyRequest, run_survey
from athena.research.wiring import (
    EMBEDDING_MODEL_ENV,
    GHOSTSCRIPT_ENV,
    VISION_MODEL_ENV,
    build_research_stack,
    build_research_tools,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
COLUMN = 22


def load_env() -> None:
    """先读 CWD 的 .env，再用项目根目录的 .env 兜底（先加载的优先）。"""
    load_dotenv()
    load_dotenv(PROJECT_ROOT / ".env")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m athena.research", description="Athena Academic Survey 全链路"
    )
    parser.add_argument("--query", default="", help="调研主题")
    parser.add_argument(
        "--papers",
        default="",
        help="逗号分隔的 arXiv id，给了就跳过检索直接取源（用于单独测量下游各段）",
    )
    parser.add_argument("--max-papers", type=int, default=50, help="交给下游的篇数")
    parser.add_argument("--max-steps", type=int, default=6, help="PaperScout 步数上限")
    parser.add_argument("--search-top-k", type=int, default=10, help="每次检索取回数")
    parser.add_argument("--max-seconds", type=float, default=600.0, help="检索墙钟预算")
    parser.add_argument(
        "--retain-threshold",
        type=float,
        default=RETAIN_THRESHOLD,
        help=(
            f"交付门槛，默认 {RETAIN_THRESHOLD}（整池按相关性排序，由 --max-papers 截断）。"
            "打分离散，非零门槛只有三档：>0.45 只要 3 分，>0.2 含 2 分，>0 含 1 分"
        ),
    )
    parser.add_argument(
        "--allow-unfetchable",
        action="store_true",
        help=(
            "让取不到源的论文也参与交付（默认剔除：既无 arXiv id、上游也没给开放获取"
            "链接的论文下载不到，却会占掉一个交付名额）"
        ),
    )
    parser.add_argument("--published-to", default="", help="发布日期上限 ISO")
    parser.add_argument(
        "--prefer", default="tex", choices=["tex", "pdf", "both"], help="取源偏好"
    )
    parser.add_argument(
        "--visual-policy",
        default="best_effort",
        choices=["best_effort", "required"],
        help="视觉解读严格度",
    )
    parser.add_argument("--concurrency", type=int, default=2, help="并发转换篇数")
    parser.add_argument(
        "--strict-quality",
        action="store_true",
        help="只把 pass / pass_with_notes 的论文放进语料（默认放行 degraded）",
    )
    parser.add_argument("--no-index", action="store_true", help="跳过建索引")
    parser.add_argument("--artifact-root", default="", help="artifact 根目录")
    parser.add_argument("--model", default="", help="策略与打分模型")
    parser.add_argument("--out", default="", help="把 SurveyReport JSON 写到该路径")
    parser.add_argument("--check", action="store_true", help="只做装配自检")
    return parser


def report_line(label: str, value: object) -> str:
    return f"  {label.ljust(COLUMN)}{value}"


def print_check(argv_model: str, artifact_root: str) -> int:
    """装配依赖并报告可用能力；缺能力时说明后果而不是直接失败。"""
    stack = build_research_stack(artifact_root=artifact_root, model=argv_model)
    tools = build_research_tools(stack)
    print("装配结果")
    print(
        report_line(
            "artifact 根目录", stack.artifacts.path_for("sha256:" + "0" * 64).parents[1]
        )
    )
    print(report_line("文本模型", stack.model or "（未设置）"))
    embedder = stack.embedder
    print(
        report_line(
            "编码器",
            embedder.model if embedder else f"关闭（未设 {EMBEDDING_MODEL_ENV}）",
        )
    )
    interpreter = stack.visual_interpreter
    print(
        report_line(
            "视觉模型",
            interpreter.model if interpreter else f"关闭（未设 {VISION_MODEL_ENV}）",
        )
    )
    print(
        report_line(
            "Ghostscript",
            stack.ghostscript or f"未找到（EPS/PS 插图读不了，可设 {GHOSTSCRIPT_ENV}）",
        )
    )
    print(report_line("联系邮箱", stack.contact_email or "（未设置，礼貌池不生效）"))
    print(
        report_line(
            "S2 key", "已设置" if stack.semantic_scholar_api_key else "无（会零星 429）"
        )
    )
    print(report_line("工具", ", ".join(item.name for item in tools.specs)))
    if not stack.model:
        print("\n缺少文本模型：设置 ATHENA_RESEARCH_MODEL 或 ATHENA_TUI_MODEL。")
        return 1
    if embedder is None:
        print(f"\n提示：未设 {EMBEDDING_MODEL_ENV}，语义检索不可用，关键词检索照常。")
    if interpreter is None:
        print(f"提示：未设 {VISION_MODEL_ENV}，图表退回仅证据文本。")
    return 0


def print_report(report: SurveyReport) -> None:
    """把成本账打成人能读的形式。"""
    timings = report.timings
    print(f"\n查询：{report.query}")
    print(report_line("状态", report.status))
    print(
        report_line(
            "候选池 / 交付",
            f"{report.scout_pool} / {report.scout_retained}"
            f"（门槛 {report.retain_threshold}）",
        )
    )
    if report.scout_dropped_no_source:
        print(
            report_line(
                "无源剔除",
                f"{report.scout_dropped_no_source} 篇过线但取不到源，未占交付名额",
            )
        )
    print(
        report_line(
            "取源成功", f"{report.fetched} / {report.fetched + report.fetch_failed}"
        )
    )
    print(report_line("转换成功", f"{report.converted()} / {report.fetched}"))
    print(report_line("转换失败率", f"{report.conversion_failure_rate():.1%}"))
    print(
        report_line(
            "静默丢失", f"{report.suspect_empty_count()} 篇（转换成功但正文近乎为空）"
        )
    )
    if report.score_histogram:
        buckets = "  ".join(
            f"{score}:{count}" for score, count in report.score_histogram.items()
        )
        print(report_line("池分数分布", buckets))
    print(report_line("语料引用", report.corpus_ref or "（未建索引）"))
    print(
        report_line(
            "耗时(秒)",
            f"scout={timings.scout_seconds} source={timings.source_seconds} "
            f"markdown={timings.markdown_seconds} index={timings.index_seconds} "
            f"total={timings.total_seconds}",
        )
    )
    print(
        report_line(
            "调用数",
            f"http={report.http_requests} vision={report.vision_calls}"
            f"(失败 {report.vision_failures}) embed={report.embed_calls}"
            f"/{report.embedded_texts} 条",
        )
    )
    print("\n逐篇：")
    for item in report.papers:
        flag = "✓" if item.conversion_status == "converted" else "✗"
        if item.suspect_empty:
            flag = "!"
        print(
            f"  {flag} {item.paper_key}  取源={item.fetch_status}/{item.source_kind or '-'}"
            f"  质量={item.quality_status or '-'}  正文={item.markdown_chars}字"
            f"  chunk={item.chunks}  图={item.visuals_interpreted}/{item.visuals}"
            f"  {item.conversion_seconds}s"
        )
        if item.suspect_empty:
            print("      正文体量异常小，判定为静默丢失，已排除出语料")
        if item.error:
            print(f"      {item.error[:200]}")
        if item.quality_codes:
            print(f"      质量码: {', '.join(item.quality_codes[:6])}")


async def main_async(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env()
    if args.check:
        return print_check(args.model, args.artifact_root)
    arxiv_ids = [item.strip() for item in args.papers.split(",") if item.strip()]
    if not args.query.strip() and not arxiv_ids:
        print("需要 --query 或 --papers，或用 --check 只做装配自检。", file=sys.stderr)
        return 2

    stack = build_research_stack(artifact_root=args.artifact_root, model=args.model)
    if not stack.model:
        print(
            "缺少文本模型：设置 ATHENA_RESEARCH_MODEL 或 ATHENA_TUI_MODEL。",
            file=sys.stderr,
        )
        return 1
    request = SurveyRequest(
        query=args.query or "direct fetch",
        arxiv_ids=arxiv_ids,
        max_papers=args.max_papers,
        max_steps=args.max_steps,
        search_top_k=args.search_top_k,
        max_seconds=args.max_seconds,
        retain_threshold=args.retain_threshold,
        require_retrievable_source=not args.allow_unfetchable,
        published_to=args.published_to,
        prefer=args.prefer,
        visual_policy=args.visual_policy,
        conversion_concurrency=args.concurrency,
        strict_quality=args.strict_quality,
        build_index=not args.no_index,
    )
    report = await run_survey(stack, request)
    print_report(report)
    if args.out:
        Path(args.out).write_text(
            json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n报告已写入 {args.out}")
    return 0 if report.converted() else 1


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(argv))
    except KeyboardInterrupt:
        # 用户中断（检索或转换阶段可能跑很久）→ 静默退出
        return 130


if __name__ == "__main__":
    sys.exit(main())
