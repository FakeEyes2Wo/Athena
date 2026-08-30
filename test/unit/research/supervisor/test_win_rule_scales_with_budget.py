"""判胜阈值必须随搜索预算变严，且 SOTA 迁移不得冒充"证明了改进"。

真机（2026-08-30，4 个实验的预算）：冠军 `hyp_a058326db0e8` 拿到 0.8716，参照
基线 0.8535，而评估器自己报的标准误是 0.0200。``settle_statistically`` 在
``family_size=1`` 时就已经判 INCONCLUSIVE——区间 0.8716 ± 1.96×0.0200 完整覆盖了
基线。**SOTA 指针照样迁了过去**，走的是另一条 ``_compare_metric`` 点估计路径
（+0.0180 > tolerance 0.005 → WIN），而这个分歧没有任何一处被记下来。

下游把"SOTA 迁移了"读成"闭环找到了改进"。留出集后来给出了答案：ΔPR-AUC 的 95%
置信区间 [−0.0102, +0.0279]，含 0。

预算与阈值的关系是这条的另一半。纯噪声（σ≈0.0074）下按 search 分挑冠军：
N=4 期望 +0.0076，N=16 是 +0.0131，N=64 是 +0.0174——最后一个已经和真机冠军实际
拿到的 +0.0180 一样大。阈值不随 N 收紧的话，加预算买到的只是更好看的数字。
"""

from statistics import NormalDist

import pytest

from athena.research.supervisor.statistics import (
    MetricEvidence,
    settle_statistically,
    two_sided_p_value,
)

# 真机数字，不是编的：见 run6_part6_before_recovery.log:3666
BASELINE = 0.8535236398328007
SOTA = 0.8715724358296061
SOTA_SE = 0.01998218647781065
SOTA_N = 14703


def test_the_real_sota_was_never_significant_at_any_budget() -> None:
    for family_size in (1, 4, 8, 16, 32):
        assert (
            settle_statistically(
                MetricEvidence(SOTA, SOTA_SE, SOTA_N),
                BASELINE,
                direction="maximize",
                min_effect_size=0.005,
                alpha=0.05,
                family_size=family_size,
            )
            == "INCONCLUSIVE"
        ), f"family_size={family_size}"


def test_a_bigger_budget_demands_a_bigger_effect() -> None:
    """同一个效应量，预算越大越判不动——这正是多重比较校正该有的行为。"""
    se, n = 0.004, 20_000
    # 一个刚好在 N=1 下够格的候选
    candidate = BASELINE + 1.96 * se + 0.001

    def verdict(family_size: int) -> str:
        return settle_statistically(
            MetricEvidence(candidate, se, n),
            BASELINE,
            direction="maximize",
            alpha=0.05,
            family_size=family_size,
        )

    assert verdict(1) == "SUPPORTED"
    assert verdict(8) == "INCONCLUSIVE"
    assert verdict(16) == "INCONCLUSIVE"

    # 效应够大时，预算再大也判得动。
    strong = BASELINE + 4.0 * se
    assert (
        settle_statistically(
            MetricEvidence(strong, se, n),
            BASELINE,
            direction="maximize",
            alpha=0.05,
            family_size=16,
        )
        == "SUPPORTED"
    )


@pytest.mark.parametrize("family_size", [1, 4, 8, 16, 32, 64])
def test_the_threshold_is_monotone_in_the_budget(family_size: int) -> None:
    """把阈值反解出来，检查它确实随 N 单调变严（Bonferroni）。"""
    se, n = 0.004, 20_000

    def supported(delta: float) -> bool:
        return (
            settle_statistically(
                MetricEvidence(BASELINE + delta, se, n),
                BASELINE,
                direction="maximize",
                alpha=0.05,
                family_size=family_size,
            )
            == "SUPPORTED"
        )

    expected = NormalDist().inv_cdf(1 - 0.05 / family_size / 2) * se
    assert not supported(expected * 0.99)
    assert supported(expected * 1.01)


def test_the_p_value_is_uncorrected_and_matches_the_z_score() -> None:
    """校正加在判据上，不能在 p 值里再加一次，否则等于校正了两遍。"""
    p = two_sided_p_value(SOTA, BASELINE, SOTA_SE)
    assert p is not None
    z = (SOTA - BASELINE) / SOTA_SE
    assert p == pytest.approx(2 * (1 - NormalDist().cdf(abs(z))), rel=1e-12)
    # 0.37：这条被当作"闭环的成果"报出去的假设，证据强度是这个数。
    assert 0.36 < p < 0.38


def test_no_uncertainty_means_no_p_value() -> None:
    assert two_sided_p_value(SOTA, BASELINE, None) is None
    assert two_sided_p_value(SOTA, BASELINE, 0.0) is None
