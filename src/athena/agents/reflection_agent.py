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
from dataclasses import dataclass, field

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.core.contracts import ArtifactStore
from athena.core.bundle import DirectoryBundle


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


@dataclass(frozen=True)
class ReviewVerdict:
    """评审通过判定结果（最小合同，不携带领域对象）。"""

    passed: bool
    failures: list[str] = field(default_factory=list)


async def evaluate_data_analysis_review(
    store: ArtifactStore, review_ref: str
) -> ReviewVerdict:
    """按 rubric 阈值判定 Review Bundle 是否通过（确定性，无模型调用）。

    读取 Reflection 提交的 Review Bundle（rubric.json + score.json），校验每个
    required criterion 的分数 >= pass_threshold 且总分 >= overall_pass_threshold，
    返回 passed/failed。不编写 rubric、不解释报告、不选择下一 Agent。

    score 必须绑定 rubric_version；版本不一致按 failed 处理并给出原因。
    """
    files = await DirectoryBundle.files(store, review_ref)
    rubric = json.loads(await store.get_text(files["rubric.json"]))
    score = json.loads(await store.get_text(files["score.json"]))

    if score.get("rubric_version") != rubric.get("rubric_version"):
        return ReviewVerdict(
            passed=False,
            failures=[f"rubric_version mismatch: {score.get('rubric_version')}"],
        )

    failures: list[str] = []
    for criterion in rubric.get("criteria", []):
        if not criterion.get("required"):
            continue
        criterion_id = criterion["criterion_id"]
        if score.get("scores", {}).get(criterion_id, 0) < criterion.get(
            "pass_threshold", 1
        ):
            failures.append(criterion_id)

    if sum(score.get("scores", {}).values()) < rubric.get("overall_pass_threshold", 0):
        failures.append("overall")

    return ReviewVerdict(passed=not failures, failures=failures)
