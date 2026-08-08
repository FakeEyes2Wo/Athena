"""确定性 EvaluationPolicy（设计 data-analysis-agent-workflow §2.4、end-to-end §5.2）。

读取 Reflection 提交的 Review Bundle（rubric.json + score.json），校验每个
required criterion 的分数 >= pass_threshold 且总分 >= overall_pass_threshold，
返回 passed/failed。不编写 rubric、不解释报告、不选择下一 Agent。

score 必须绑定 rubric_version；版本不一致按 failed 处理并给出原因。
"""

import json
from dataclasses import dataclass, field

from athena.storage.artifact_store import ArtifactStore
from athena.storage.bundle import DirectoryBundle


@dataclass(frozen=True)
class ReviewVerdict:
    """评审通过判定结果（最小合同，不携带领域对象）。"""

    passed: bool
    failures: list[str] = field(default_factory=list)


async def evaluate_data_analysis_review(
    store: ArtifactStore, review_ref: str
) -> ReviewVerdict:
    """按 rubric 阈值判定 Review Bundle 是否通过（确定性，无模型调用）。"""
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


if __name__ == "__main__":
    import asyncio
    import tempfile

    from athena.storage.artifact_store import LocalArtifactStore

    async def demo() -> None:
        store = LocalArtifactStore(tempfile.mkdtemp())
        rubric = {"rubric_version": 1, "overall_pass_threshold": 1, "criteria": []}
        score = {"rubric_version": 1, "scores": {}, "evidence": {}}
        review_ref = await DirectoryBundle.commit(
            store,
            {
                "rubric.json": await store.put_text(json.dumps(rubric)),
                "score.json": await store.put_text(json.dumps(score)),
                "review.md": await store.put_text("demo"),
            },
        )
        verdict = await evaluate_data_analysis_review(store, review_ref)
        print(f"evaluate_data_analysis_review -> passed={verdict.passed}")

    asyncio.run(demo())
