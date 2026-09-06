"""风险阈值的实测校准（2026-08-15，MazeCrawler）。

初版 MAX_TOLERATED_RISKS=2 的依据是"审阅几乎总能挑出一两条风险"这个假设。真实跑测
推翻了它：每份审阅稳定产出 4-5 条（观测 4,4,5,5,5,5,5,5，从未 ≤2），且
fatal_flaw_found 全为 False——审阅者并不认为有致命缺陷。阈值 2 因此不是"严"，是数学上
无人可过，连续 3 轮重新提案全部卡死、SEARCH 一个实验都跑不成。

本文件把这批实测数字固化成回归：让阈值再被改回"无人可过"的量级时测试会红。
"""

import pytest

from athena.research.idea_generation.gatekeeper import (
    MAX_TOLERATED_RISKS,
    max_total_risks,
    perspective_ok,
)
from athena.research.idea_generation.idea_schemas import SkepticReport

# MazeCrawler 真实跑测里每份审阅报告的风险条数。
OBSERVED_RISK_COUNTS = (4, 4, 5, 5, 5, 5, 5, 5)


def _review(risks: int, *, fatal: bool = False, failed: bool = False) -> SkepticReport:
    return SkepticReport(
        idea_id="idea-1",
        perspective="methodology",
        critique="c",
        unaddressed_risks=[f"risk {i}" for i in range(risks)],
        fatal_flaw_found=fatal,
        failed=failed,
    )


@pytest.mark.parametrize("risks", OBSERVED_RISK_COUNTS)
def test_observed_review_volume_is_not_blocked_by_itself(risks):
    """实测量级的风险条数不得单凭数量就拦下候选——否则没有候选能通过。"""
    assert perspective_ok(_review(risks)) is True


def test_a_reviewer_declaring_a_fatal_flaw_still_blocks():
    """判定主依据是审阅者自己的裁定，不是条数：fatal 一票否决。"""
    assert perspective_ok(_review(1, fatal=True)) is False


def test_a_failed_review_still_blocks():
    """fail-closed：审阅没跑成不能等于审阅批准。"""
    assert perspective_ok(_review(0, failed=True)) is False


def test_runaway_risk_volume_still_blocks():
    """条数仍是"明显失控"的兵线，只是兵线抬高了。"""
    assert perspective_ok(_review(MAX_TOLERATED_RISKS + 1)) is False


# ====== 跨视角总量 ======


def test_total_ceiling_admits_the_observed_two_perspective_volume():
    """实测双视角总量 9-10，必须落在放行区间内。"""
    assert max_total_risks(2) >= 10


def test_total_ceiling_preserves_the_non_dead_code_invariant():
    """总量上限必须严格小于 单项上限 × 视角数，否则 risk_total 恒满分、是死代码。"""
    for count in (1, 2, 3, 8):
        assert max_total_risks(count) < MAX_TOLERATED_RISKS * count


def test_total_ceiling_scales_with_perspective_count():
    """初版写死成 MAX_TOLERATED_RISKS+1，单项调高后会低于实测总量，把放宽效果吃掉。"""
    assert max_total_risks(3) > max_total_risks(2) > max_total_risks(1)


def test_total_ceiling_rejects_a_meaningless_perspective_count():
    with pytest.raises(ValueError, match="at least 1"):
        max_total_risks(0)
