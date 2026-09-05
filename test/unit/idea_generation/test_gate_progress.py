"""门禁必须报告进度：它全程只有 LLM 往返，静默时"正在跑"与"卡死"无法区分。

本次会话在 MazeCrawler 真实跑测里据此误判过一次——Ideator 出完假设后 21 分钟无输出、
无 artifact 新增，从外部完全看不出门禁是否还在工作。
"""

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.idea_generation import gate as gate_module
from athena.research.idea_generation.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    FalsifiabilityJudgment,
    IdeatorHypothesisDraft,
    SkepticJudgment,
)


def _draft(statement: str = "s") -> IdeatorHypothesisDraft:
    return IdeatorHypothesisDraft(
        statement=statement,
        intervention="i",
        expected_effect="e",
        supported_premises=[
            ClaimEvidence(
                claim="c", role=ClaimRole.SUPPORTED_PREMISE, supporting_refs=["r"]
            )
        ],
        predicted_observations=["p"],
        disconfirming_observations=["d"],
    )


def _patch(monkeypatch, *, falsifiable: bool = True):
    async def _fake_chat(prompt, schema, *, model, artifacts, **kwargs):
        if schema is FalsifiabilityJudgment:
            return FalsifiabilityJudgment(
                testable_implication="t" if falsifiable else "",
                unobservable_variables=[],
                is_falsifiable=falsifiable,
            )
        if schema is SkepticJudgment:
            return SkepticJudgment(
                critique="c", unaddressed_risks=[], fatal_flaw_found=False
            )
        raise AssertionError(f"unexpected schema: {schema}")

    for path in (
        "athena.research.idea_generation.gate",
        "athena.research.idea_generation.review_board",
    ):
        monkeypatch.setattr(f"{path}.single_turn_structured_chat", _fake_chat)


@pytest.mark.asyncio
async def test_progress_is_reported_around_every_llm_stage(tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path / "artifacts")
    _patch(monkeypatch)
    seen: list[str] = []

    async def progress(message: str) -> None:
        seen.append(message)

    await gate_module.run_light_pipeline(
        [_draft()], model="m", artifacts=store, progress=progress
    )

    joined = " | ".join(seen)
    assert "falsifiability check" in joined
    assert "review" in joined
    assert "kept 1/1" in joined


@pytest.mark.asyncio
async def test_dropping_a_candidate_reports_why(tmp_path, monkeypatch):
    """被丢弃的候选必须留下可读的原因，否则它就是静默消失。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    _patch(monkeypatch, falsifiable=False)
    seen: list[str] = []

    async def progress(message: str) -> None:
        seen.append(message)

    kept = await gate_module.run_light_pipeline(
        [_draft()], model="m", artifacts=store, progress=progress
    )

    assert kept == []
    joined = " | ".join(seen)
    assert "pre_gate dropped" in joined
    assert "falsifiable" in joined
    assert "kept 0/1" in joined


@pytest.mark.asyncio
async def test_progress_defaults_to_silent(tmp_path, monkeypatch):
    """不传 progress 时不得报错——库内调用与单测不需要观测面。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    _patch(monkeypatch)
    kept = await gate_module.run_light_pipeline([_draft()], model="m", artifacts=store)
    assert len(kept) == 1
