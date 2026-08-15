"""VerifierRegistry+ValidationPlanner（验证方案，步骤 [7]）。VerifierRegistry 是纯规则匹配
（无 LLM），P1+P2 阶段先内置一个小列表，不接外部服务；找不到匹配就返回 None，交给
ValidationPlanner 标 EXPLORATORY，不得虚构 verifier。
"""

import json

from athena.core.contracts import ArtifactStore
from athena.research.idea_generation.idea_schemas import HypothesisPackage, ValidationPlan, VerifierSpec


# ====== 常量：内置 verifier 列表 ======

BUILTIN_VERIFIERS: tuple[VerifierSpec, ...] = (
    VerifierSpec(
        verifier_type="ablation_replication",
        applicable_domains=["machine_learning", "ai4s"],
        observable_vars=["metric_before_ablation", "metric_after_ablation"],
        statistical_assumptions=["same random seed budget as baseline", "same evaluation split"],
        success_condition="metric change matches expected_effect direction beyond noise floor",
        failure_condition="metric change is within noise floor or in the opposite direction",
        inconclusive_condition="baseline run itself failed to reproduce",
        cost_ref="unestimated",
        supports_auto_exec=True,
        requires_human_approval=False,
    ),
)


# ====== VerifierRegistry（步骤 [7]，纯规则匹配） ======

def match_verifier(package: HypothesisPackage, domain: str) -> VerifierSpec | None:
    """按研究领域从内置列表匹配一个可用 verifier；纯函数，不调用外部服务。找不到就返回
    None，交给 plan_validation 标 EXPLORATORY，不得虚构 verifier。

    Example:
        >>> match_verifier(package, "biology").verifier_type  # doctest: +SKIP
        'controlled_experiment_ttest'
        >>> match_verifier(package, "underwater_basket_weaving") is None  # doctest: +SKIP
        True
    """
    normalized_domain = domain.strip().lower().replace(" ", "_")
    for verifier in BUILTIN_VERIFIERS:
        if normalized_domain in verifier.applicable_domains:
            return verifier
    return None


# ====== ValidationPlanner（步骤 [7]） ======

async def plan_validation(
    package: HypothesisPackage,
    verifier: VerifierSpec | None,
    *,
    artifacts: ArtifactStore,
) -> ValidationPlan:
    """verifier 为 None 时标 EXPLORATORY，不得虚构 verifier；否则把 verifier 的
    placeholder cost_ref 换成真实落盘的成本估计引用。

    Example:
        >>> plan = await plan_validation(package, None, artifacts=store)  # doctest: +SKIP
        >>> plan.verifier is None
        True
    """
    if verifier is None:
        cost_ref = await artifacts.put_text(
            json.dumps({"note": "no verifier matched; exploratory only"})
        )
        return ValidationPlan(
            idea_id=package.idea_id,
            minimal_test=(
                f"No registered verifier matches this hypothesis; treat {package.idea_id} as "
                "EXPLORATORY pending manual verifier design."
            ),
            verifier=None,
            decision_rule="EXPLORATORY: no automatic pass/fail rule until a verifier is designed.",
            estimated_cost_ref=cost_ref,
        )

    cost_ref = await artifacts.put_text(json.dumps({
        "verifier_type": verifier.verifier_type,
        "supports_auto_exec": verifier.supports_auto_exec,
        "requires_human_approval": verifier.requires_human_approval,
    }))
    bound_verifier = verifier.model_copy(update={"cost_ref": cost_ref})
    return ValidationPlan(
        idea_id=package.idea_id,
        minimal_test=f"Run {verifier.verifier_type} comparing {', '.join(verifier.observable_vars)}.",
        verifier=bound_verifier,
        decision_rule=(
            f"PASS if: {verifier.success_condition}. FAIL if: {verifier.failure_condition}. "
            f"INCONCLUSIVE if: {verifier.inconclusive_condition}."
        ),
        estimated_cost_ref=cost_ref,
    )
