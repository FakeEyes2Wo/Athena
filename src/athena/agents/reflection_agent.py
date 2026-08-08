"""只读评审 Agent（设计 data-analysis-agent-workflow §3.3）。

先写 rubric 再按 rubric 逐项评分，输出独立评审 Bundle：:

    DataAnalysisReview/
      rubric.json
      score.json
      review.md

不能修改被评 Bundle，也不能提交新的 DataAnalysis 版本。请求带
``data_analysis_ref`` 时按数据 rubric（report + figure）评审，带
``report_ref`` 时按报告 rubric（report 非空）评审。首版为确定性实现，
产出可供程序读取的 rubric/score JSON，EvaluationPolicy 据此判定通过。
"""

import json

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.storage.artifact_store import ArtifactStore
from athena.storage.bundle import DirectoryBundle


class ReflectionAgent(BaseAgent):
    """只读评审 Agent（确定性实现）。"""

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """按请求评审对象产出 rubric/score/review Bundle，返回其 ref。"""
        request = json.loads(ctx.input_text or "{}")
        if request.get("report_ref") is not None:
            return await self._review_report(request["report_ref"])
        return await self._review_data_analysis(request.get("data_analysis_ref"))

    async def _review_data_analysis(self, data_ref: str | None) -> AgentOutcome:
        report = ""
        has_figure = False
        if data_ref is not None:
            files = await DirectoryBundle.files(self._store, data_ref)
            report_ref = files.get("report.md")
            if report_ref is not None:
                report = await self._store.get_text(report_ref)
            has_figure = any(p.startswith("figures/") for p in files)

        rubric = {
            "rubric_version": 1,
            "overall_pass_threshold": 2,
            "criteria": [
                {
                    "criterion_id": "report_nonempty",
                    "score_min": 0,
                    "score_max": 1,
                    "pass_threshold": 1,
                    "required": True,
                },
                {
                    "criterion_id": "figure_present",
                    "score_min": 0,
                    "score_max": 1,
                    "pass_threshold": 1,
                    "required": True,
                },
            ],
        }
        scores = {
            "report_nonempty": 1 if report.strip() else 0,
            "figure_present": 1 if has_figure else 0,
        }
        evidence = {
            "report_nonempty": f"report 长度 {len(report)}",
            "figure_present": f"figures 存在={has_figure}",
        }
        return await self._commit(
            rubric, scores, evidence, f"data review: 长度 {len(report)}"
        )

    async def _review_report(self, report_ref: str) -> AgentOutcome:
        report = ""
        if report_ref is not None:
            files = await DirectoryBundle.files(self._store, report_ref)
            report_md = files.get("report.md")
            if report_md is not None:
                report = await self._store.get_text(report_md)

        rubric = {
            "rubric_version": 1,
            "overall_pass_threshold": 1,
            "criteria": [
                {
                    "criterion_id": "report_nonempty",
                    "score_min": 0,
                    "score_max": 1,
                    "pass_threshold": 1,
                    "required": True,
                }
            ],
        }
        scores = {"report_nonempty": 1 if report.strip() else 0}
        evidence = {"report_nonempty": f"report 长度 {len(report)}"}
        return await self._commit(
            rubric, scores, evidence, f"report review: 长度 {len(report)}"
        )

    async def _commit(
        self, rubric: dict, scores: dict, evidence: dict, review: str
    ) -> AgentOutcome:
        review_files = {
            "rubric.json": await self._store.put_text(
                json.dumps(rubric, ensure_ascii=False)
            ),
            "score.json": await self._store.put_text(
                json.dumps(
                    {
                        "rubric_version": rubric["rubric_version"],
                        "scores": scores,
                        "evidence": evidence,
                    },
                    ensure_ascii=False,
                )
            ),
            "review.md": await self._store.put_text(review),
        }
        review_ref = await DirectoryBundle.commit(self._store, review_files)
        return AgentOutcome(result_ref=review_ref)
