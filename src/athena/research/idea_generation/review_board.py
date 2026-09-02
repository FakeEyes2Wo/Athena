"""多视角并行审阅（ReviewBoard）：两个各自独立上下文的视角，每个视角在
light_hard_gate 里各占一项 rubric（risk_ok_<perspective>）。

呼应 Co-Scientist"生成与审阅分离、审阅侧不看生成侧自评概率"的设计：两个视角的 prompt
都不携带 sampling_probability。
"""

from contextlib import nullcontext
from dataclasses import dataclass

from athena.core.contracts import ArtifactStore
from athena.research.idea_generation.idea_schemas import (
    HypothesisPackage,
    SkepticJudgment,
    SkepticReport,
)
from athena.research.idea_generation.prompts import (
    REVIEW_METHODOLOGY_SYSTEM_PROMPT,
    REVIEW_PERSPECTIVE_HEADER_TEMPLATE,
    REVIEW_STATISTICS_SYSTEM_PROMPT,
    SKEPTIC_REVIEW_USER_PROMPT_TEMPLATE,
)
from athena.research.idea_generation.structured_chat import single_turn_structured_chat


@dataclass(frozen=True)
class ReviewPerspective:
    """一个审阅视角：id 同时用作 rubric 项后缀 risk_ok_<perspective_id>。

    Example:
        >>> ReviewPerspective("methodology", "system prompt").perspective_id
        'methodology'
    """

    perspective_id: str
    system_prompt: str


REVIEW_PERSPECTIVES: tuple[ReviewPerspective, ...] = (
    ReviewPerspective("methodology", REVIEW_METHODOLOGY_SYSTEM_PROMPT),
    ReviewPerspective("statistics", REVIEW_STATISTICS_SYSTEM_PROMPT),
)
"""视角顺序是稳定的：light_hard_gate 按此顺序扫描以确定 blocking_factor。"""


def format_premise_lines(package: HypothesisPackage) -> str:
    """把 supported_premises 格式化成逐行文本，供审阅 prompt 使用。

    Example:
        >>> format_premise_lines(package)  # doctest: +SKIP
        '- [supported_premise] X correlates with Y (refs: ev-0)'
    """
    return (
        "\n".join(
            f"- [{premise.role.value}] {premise.claim} (refs: {', '.join(premise.supporting_refs) or '-'})"
            for premise in package.supported_premises
        )
        or "(no supported premises)"
    )


def build_review_prompt(
    package: HypothesisPackage, perspective: ReviewPerspective
) -> str:
    """拼装单视角审阅 prompt。只取 novel_hypothesis / supported_premises /
    predicted_observations / disconfirming_observations，不传任何生成侧自评分数。

    Example:
        >>> build_review_prompt(package, REVIEW_PERSPECTIVES[0]).startswith("You are")  # doctest: +SKIP
        True
    """
    premise_lines = format_premise_lines(package)
    return "\n\n".join(
        [
            perspective.system_prompt,
            REVIEW_PERSPECTIVE_HEADER_TEMPLATE.format(
                perspective_id=perspective.perspective_id
            )
            + SKEPTIC_REVIEW_USER_PROMPT_TEMPLATE.format(
                novel_hypothesis=package.novel_hypothesis,
                supported_premises=premise_lines,
                predicted_observations="\n".join(
                    f"- {o}" for o in package.predicted_observations
                ),
                disconfirming_observations="\n".join(
                    f"- {o}" for o in package.disconfirming_observations
                ),
            ),
        ]
    )


async def review_or_degrade(
    package: HypothesisPackage,
    perspective: ReviewPerspective,
    *,
    artifacts: ArtifactStore,
    llm_sem=None,
    model: str,
) -> SkepticReport:
    """跑一次视角审阅；失败就把异常映射成 failed=True 的 SkepticReport，从不向外抛。

    Example:
        >>> report = await review_or_degrade(package, REVIEW_PERSPECTIVES[0],
        ...     artifacts=store, model="m")  # doctest: +SKIP
        >>> report.failed  # doctest: +SKIP
        False
    """
    try:
        prompt = build_review_prompt(package, perspective)
        async with llm_sem if llm_sem is not None else nullcontext():
            judgment = await single_turn_structured_chat(
                prompt,
                SkepticJudgment,
                model=model,
                artifacts=artifacts,
            )
        return SkepticReport(
            idea_id=package.idea_id,
            perspective=perspective.perspective_id,
            critique=judgment.critique,
            unaddressed_risks=judgment.unaddressed_risks,
            fatal_flaw_found=judgment.fatal_flaw_found,
            input_ref=await artifacts.put_text(prompt),
        )
    except Exception as error:  # noqa: BLE001 - provider 报错形态不定，直接降级
        return SkepticReport(
            idea_id=package.idea_id,
            perspective=perspective.perspective_id,
            critique=f"review failed: {error}",
            unaddressed_risks=[],
            fatal_flaw_found=False,
            failed=True,
        )
