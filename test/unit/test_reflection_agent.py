"""确定性 EvaluationPolicy 单元测试（设计 data-analysis-agent-workflow §2.4）。"""

import json

import pytest

from athena.evaluation.policy import evaluate_data_analysis_review
from athena.storage.artifact_store import LocalArtifactStore
from athena.storage.bundle import DirectoryBundle

RUBRIC = {
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


def _score(scores: dict) -> dict:
    return {"rubric_version": 1, "scores": scores, "evidence": {}}


async def _commit_review(store, rubric: dict, score: dict) -> str:
    return await DirectoryBundle.commit(
        store,
        {
            "rubric.json": await store.put_text(json.dumps(rubric)),
            "score.json": await store.put_text(json.dumps(score)),
            "review.md": await store.put_text("review"),
        },
    )


@pytest.mark.asyncio
async def test_passes_when_all_required_met(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    review_ref = await _commit_review(
        store, RUBRIC, _score({"report_nonempty": 1, "figure_present": 1})
    )
    verdict = await evaluate_data_analysis_review(store, review_ref)
    assert verdict.passed
    assert verdict.failures == []


@pytest.mark.asyncio
async def test_fails_on_missing_required_criterion(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    review_ref = await _commit_review(
        store, RUBRIC, _score({"report_nonempty": 0, "figure_present": 1})
    )
    verdict = await evaluate_data_analysis_review(store, review_ref)
    assert not verdict.passed
    assert "report_nonempty" in verdict.failures


@pytest.mark.asyncio
async def test_fails_on_overall_threshold(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    # 总分 1 < 2，尽管无单项缺失（非 required 满分也计入总分）
    review_ref = await _commit_review(
        store,
        {"rubric_version": 1, "overall_pass_threshold": 2, "criteria": []},
        _score({"report_nonempty": 1}),
    )
    verdict = await evaluate_data_analysis_review(store, review_ref)
    assert not verdict.passed
    assert "overall" in verdict.failures


@pytest.mark.asyncio
async def test_fails_on_rubric_version_mismatch(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    score = _score({"report_nonempty": 1, "figure_present": 1})
    score["rubric_version"] = 2  # score 绑定旧/新版本，不可与 rubric 混用
    review_ref = await _commit_review(store, RUBRIC, score)
    verdict = await evaluate_data_analysis_review(store, review_ref)
    assert not verdict.passed
    assert verdict.failures == ["rubric_version mismatch: 2"]


@pytest.mark.asyncio
async def test_ignores_non_required_criterion_failure(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    rubric = {
        "rubric_version": 1,
        "overall_pass_threshold": 1,
        "criteria": [
            {
                "criterion_id": "required_ok",
                "score_min": 0,
                "score_max": 1,
                "pass_threshold": 1,
                "required": True,
            },
            {
                "criterion_id": "optional",
                "score_min": 0,
                "score_max": 1,
                "pass_threshold": 1,
                "required": False,
            },
        ],
    }
    review_ref = await _commit_review(
        store,
        rubric,
        _score({"required_ok": 1, "optional": 0}),
    )
    verdict = await evaluate_data_analysis_review(store, review_ref)
    assert verdict.passed  # required 满足，optional 缺失不计入
