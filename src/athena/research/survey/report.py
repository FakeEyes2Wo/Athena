"""把 ``SurveyReport`` 与装配自检渲染成人能读的形式。

单独成一个模块，是为了让 ``athena.cli`` 的 ``survey`` 子命令只做参数解析与调度：
成本账的排版规则（哪些数要并排、未尝试的候选不逐条列）属于 survey 自己的知识，
放进通用 CLI 只会让那个文件跟着 ``SurveyReport`` 的字段一起变。
"""

from athena.research.survey.pipeline import NOT_ATTEMPTED, SurveyReport
from athena.research.survey.wiring import (
    EMBEDDING_MODEL_ENV,
    GHOSTSCRIPT_ENV,
    SCORER_MODEL_ENV,
    VISION_MODEL_ENV,
    SurveyStack,
    build_survey_tools,
)

COLUMN = 22


def report_line(label: str, value: object) -> str:
    """一行「标签  值」，标签列对齐到 ``COLUMN``。"""
    return f"  {label.ljust(COLUMN)}{value}"


def print_check(stack: SurveyStack) -> int:
    """报告装配结果与可用能力；缺能力时说明后果而不是直接失败。

    返回进程退出码：能力齐不齐都返回 0，因为降级形态仍然可跑；只有连文本模型
    都没有才算装配失败。
    """
    tools = build_survey_tools(stack)
    print("装配结果")
    print(
        report_line(
            "artifact 根目录", stack.artifacts.path_for("sha256:" + "0" * 64).parents[1]
        )
    )
    print(report_line("策略模型", stack.model or "（未设置）"))
    print(
        report_line(
            "打分模型",
            stack.scorer_model
            or f"同策略模型（可设 {SCORER_MODEL_ENV} 换轻量模型提速）",
        )
    )
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
        print("\n缺少文本模型：设置 ATHENA_SURVEY_MODEL 或 MODEL_NAME。")
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
    status = report.status
    if report.scout_status and report.scout_status != report.status:
        status = f"{status}（检索段自评 {report.scout_status}，原因见 warnings）"
    print(report_line("状态", status))
    print(
        report_line(
            "候选池 / 取源候选",
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
            "取源成功",
            f"{report.fetched} / {report.fetch_attempted} 次尝试"
            f"（候选 {report.scout_retained}，够数即停）",
        )
    )
    if report.surplus_dropped:
        print(
            report_line(
                "垫底富余", f"{report.surplus_dropped} 篇取到源但排在名额外，未转换"
            )
        )
    print(
        report_line(
            "转换成功",
            f"{report.converted()} / {report.fetched - report.surplus_dropped}",
        )
    )
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
    # 未尝试的候选不逐条列出：够数即停之后可能有二十来篇，它们既没失败也没花成本，
    # 混在里面只会把真正失败的那几篇淹掉
    untouched = sum(1 for item in report.papers if item.fetch_status == NOT_ATTEMPTED)
    if untouched:
        print(report_line("未尝试候选", f"{untouched} 篇（够数即停，一次都没下载）"))
    print("\n逐篇：")
    for item in report.papers:
        if item.fetch_status == NOT_ATTEMPTED:
            continue
        flag = "✓" if item.conversion_status == "converted" else "✗"
        if item.conversion_status == "surplus":
            # 多取的富余不是失败：源取到了，只是排在名额外
            flag = "·"
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
