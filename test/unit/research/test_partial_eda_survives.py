"""一份 EDA 报告失败，不该让写好的其余报告一起作废。

回归自两轮真实运行：

- 2026-08-29：EDA 九份报告全部写成，汇总环节在第一个事件上死掉，PREPARE 退回
  "EDA failed，降级到任务原文"。报告就在盘上，没有任何环节读它们。
- 2026-08-30：``EDA_REPORT_04_RELATIONSHIPS.md`` 因输出上限截断连废三次，
  ``run_eda_todos`` 返回非空 ``failed``，于是同一条路径又走了一遍——另外六份
  写得好好的报告（含对该任务最关键的 03 Target 与 05 Leaks & Drift）全部陪葬。

判定标准因此改成"还剩几份**真**报告"，而不是"有没有失败项"。
"""

from pathlib import Path

from athena.research.prepare.eda import (
    PLACEHOLDER_MARK,
    usable_eda_reports,
    write_missing_report_placeholders,
)

TODO = """# EDA TODO

## Stage 1
- [ ] 00 Overview -> EDA_REPORT_00_OVERVIEW.md

## Stage 2
- [ ] 04 Relationships -> EDA_REPORT_04_RELATIONSHIPS.md
- [ ] 05 Leaks & Drift -> EDA_REPORT_05_LEAKS_DRIFT.md
"""


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "EDA_TODO.md").write_text(TODO, encoding="utf-8")
    return tmp_path


def test_placeholder_is_not_counted_as_a_usable_report(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "EDA_REPORT_00_OVERVIEW.md").write_text(
        "# Overview\n\n真内容\n", encoding="utf-8"
    )
    (ws / "EDA_REPORT_05_LEAKS_DRIFT.md").write_text(
        "# Leaks\n\n真内容\n", encoding="utf-8"
    )

    # 04 写失败 → 补占位符
    write_missing_report_placeholders(ws)
    assert PLACEHOLDER_MARK in (ws / "EDA_REPORT_04_RELATIONSHIPS.md").read_text(
        encoding="utf-8"
    )

    usable = [p.name for p in usable_eda_reports(ws)]
    assert usable == ["EDA_REPORT_00_OVERVIEW.md", "EDA_REPORT_05_LEAKS_DRIFT.md"]
    # 这就是修复的要点：还有两份真报告，不该判定整个 EDA 失败。
    assert usable, "有真报告时不得降级"


def test_all_placeholders_means_no_usable_reports(tmp_path):
    """一份都没写成时，降级仍然是对的行为。"""
    ws = _workspace(tmp_path)
    write_missing_report_placeholders(ws)
    assert usable_eda_reports(ws) == []


def test_existing_reports_are_never_overwritten_by_placeholders(tmp_path):
    ws = _workspace(tmp_path)
    real = ws / "EDA_REPORT_00_OVERVIEW.md"
    real.write_text("# Overview\n\n不能被覆盖\n", encoding="utf-8")
    write_missing_report_placeholders(ws)
    assert "不能被覆盖" in real.read_text(encoding="utf-8")
