"""实验文档：把每个阶段的结论与派生报告落到 ``.athena/exp_docs/``。

调用方是 ``supervisor/phases.py`` 与 ``supervisor/settlement.py``：每当一个阶段
产生可信结论（PREPARE 的基线、SEARCH 的一次结算、VALIDATE 的最终评估）或一个
阶段终态失败时，写一份 stage doc，并刷新由研究树派生的两份报告。

布局::

    .athena/exp_docs/
      runs/<run_id>.json    每次结论一份，永不覆盖别人
      <stage>.json          该阶段的最新一份（baseline / search / final）
      FINAL_REPORT.md       研究树 + VALIDATE 的完整报告
      OPTIMIZATION.md       按指标排序的迭代轨迹

``runs/`` 按 run_id 留档、``<stage>.json`` 只保留最新，是因为两种读法都需要：
前者要逐条追溯，后者要"这个阶段现在是什么状态"而不必先知道 run_id。
"""

import json
from pathlib import Path
from typing import Any, Mapping

from athena.core.persistence import atomic_write_json
from athena.core.research_tree import ResearchTree
from athena.research.report import build_final_report

DOCS_DIRNAME = "exp_docs"
RUNS_DIRNAME = "runs"
FINAL_REPORT_NAME = "FINAL_REPORT.md"
OPTIMIZATION_NAME = "OPTIMIZATION.md"
DEFAULT_METRIC_NAME = "primary"
# run_id / stage 会直接拼进文件名，必须挡住路径穿越与分隔符。
_UNSAFE = ("/", "\\", "..", ":")


def _docs_root(project_root: Path | str) -> Path:
    """返回 ``.athena/exp_docs`` 目录（不创建）。"""
    return Path(project_root) / ".athena" / DOCS_DIRNAME


def _safe_name(value: str, fallback: str) -> str:
    """把 run_id/stage 收敛成一个安全的文件名主干。"""
    text = str(value).strip()
    if not text or any(token in text for token in _UNSAFE):
        return fallback
    return text


def task_metric_name(project_root: Path | str, task_understanding: Any) -> str:
    """主指标的展示名：冻结评估器 spec > 任务理解 > ``primary``。

    优先读评估器，是因为它是被冻结的权威：任务理解可能在澄清阶段写下一个口语化
    的名字，而报告里的指标名应当和真正打分的那个一致。
    """
    spec_path = Path(project_root) / ".athena" / "evaluator_spec.json"
    if spec_path.is_file():
        try:
            declared = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # spec 损坏或正被重写：退到任务理解，不该因此中断阶段结算。
            declared = {}
        name = declared.get("primary_metric")
        if isinstance(name, str) and name.strip():
            return name.strip()
    if isinstance(task_understanding, Mapping):
        name = task_understanding.get("primary_metric")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return DEFAULT_METRIC_NAME


def write_stage_doc(project_root: Path | str, doc: Mapping[str, Any]) -> None:
    """写一份阶段结论：``runs/<run_id>.json`` 与 ``<stage>.json``。"""
    root = _docs_root(project_root)
    (root / RUNS_DIRNAME).mkdir(parents=True, exist_ok=True)
    payload = dict(doc)
    run_id = _safe_name(str(payload.get("run_id") or ""), "run")
    stage = _safe_name(str(payload.get("stage") or ""), "stage")
    atomic_write_json(root / RUNS_DIRNAME / f"{run_id}.json", payload)
    atomic_write_json(root / f"{stage}.json", payload)


def _optimization_report(
    tree: ResearchTree,
    validation: Mapping[str, Any] | None,
    *,
    metric_name: str,
    direction: str,
) -> str:
    """按指标排序渲染迭代轨迹，标出当前 SOTA。"""
    data = tree.to_dict()
    experiments = data.get("experiments") or {}
    sota_id = data.get("sota_id")
    lines = [
        "# 优化轨迹",
        "",
        f"指标：`{metric_name}`（{direction}）",
        "",
    ]
    scored: list[tuple[float, str, dict]] = []
    unscored: list[tuple[str, dict]] = []
    for experiment_id, experiment in experiments.items():
        primary = ((experiment.get("eval") or {}) or {}).get("primary")
        if isinstance(primary, (int, float)):
            scored.append((float(primary), experiment_id, experiment))
        else:
            unscored.append((experiment_id, experiment))
    scored.sort(key=lambda item: item[0], reverse=direction != "minimize")

    lines += ["| 排名 | 实验 | 指标 | 状态 | SOTA |", "|---|---|---|---|---|"]
    for rank, (primary, experiment_id, experiment) in enumerate(scored, start=1):
        mark = "是" if experiment_id == sota_id else ""
        lines.append(
            f"| {rank} | `{experiment_id}` | {primary:.6f} | "
            f"{experiment.get('status', '')} | {mark} |"
        )
    if not scored:
        lines.append("| — | 尚无带分数的实验 | — | — | — |")
    if unscored:
        lines += ["", "## 未产出分数", ""]
        for experiment_id, experiment in unscored:
            reason = experiment.get("error") or experiment.get("status") or "未知"
            lines.append(f"- `{experiment_id}`：{reason}")
    if validation:
        final_score = validation.get("final_test_score")
        lines += [
            "",
            "## VALIDATE",
            "",
            f"- final_test_score：{final_score}",
            f"- search 参考：{validation.get('test_score')}",
            f"- 泛化差：{validation.get('generalization_gap')}",
        ]
    return "\n".join(lines) + "\n"


def write_reports(
    project_root: Path | str,
    tree: ResearchTree,
    validation: Mapping[str, Any] | None = None,
    *,
    metric_name: str = DEFAULT_METRIC_NAME,
    direction: str = "maximize",
) -> None:
    """刷新由研究树派生的两份报告。

    每次阶段结算都整份重写，而不是追加：报告是研究树的投影，追加会让它和树的
    真实状态漂移，而树本身才是单一事实来源。
    """
    root = _docs_root(project_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / FINAL_REPORT_NAME).write_text(
        build_final_report(tree, validation), encoding="utf-8"
    )
    (root / OPTIMIZATION_NAME).write_text(
        _optimization_report(
            tree, validation, metric_name=metric_name, direction=direction
        ),
        encoding="utf-8",
    )
