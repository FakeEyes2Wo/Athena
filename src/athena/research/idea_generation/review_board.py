"""多视角并行审阅（ReviewBoard）：两个各自独立上下文的视角，每个视角在
light_hard_gate 里各占一项 rubric（risk_ok_<perspective>）。

呼应 Co-Scientist"生成与审阅分离、审阅侧不看生成侧自评概率"的设计：两个视角的 prompt
都不携带 sampling_probability。
"""

from contextlib import nullcontext
from dataclasses import dataclass

from athena.core.agent.chat import single_turn_structured_chat
from athena.core.contracts import ArtifactStore
from athena.research.idea_generation.idea_schemas import (
    IdeatorHypothesisDraft,
    SkepticJudgment,
    SkepticReport,
)

_REVIEW_USER_PROMPT = (
    "Novel hypothesis: {novel_hypothesis}\n"
    "Supported premises:\n{supported_premises}\n"
    "Predicted observations:\n{predicted_observations}\n"
    "Disconfirming observations:\n{disconfirming_observations}\n\n"
    "Provide an independent critique."
)

_METHODOLOGY_PROMPT = (
    "You are an independent methodology reviewer on a review board. You are given a "
    "hypothesis package without any self-assessed confidence score from its generator, "
    "precisely so you do not anchor on it. Restrict your critique to methodology: "
    "whether a control or baseline is clearly defined, whether confounding variables "
    "are accounted for, whether the claim conflates correlation with causation, and "
    "whether the stated intervention is actually operable as written. Do not reject a "
    "hypothesis merely because it departs from the baseline or replaces the "
    "architecture; judge whether it is well-defined and falsifiable. Do not comment "
    "on statistical power or on agreement with published literature - other reviewers "
    "cover those. Report only risks that fall within methodology. Set fatal_flaw_found "
    "to true only if the flaw cannot be fixed by revising the hypothesis."
)

_STATISTICS_PROMPT = (
    "You are an independent statistical-validity reviewer on a review board. You are "
    "given a hypothesis package without any self-assessed confidence score from its "
    "generator, precisely so you do not anchor on it. Restrict your critique to "
    "statistical validity: sample size and power, multiple-comparison exposure, whether "
    "the expected effect is distinguishable from the noise floor, and whether the "
    "predicted observations are quantifiable at all. Do not comment on experimental "
    "design choices or on agreement with published literature - other reviewers cover "
    "those. Report only risks that fall within statistical validity. Set "
    "fatal_flaw_found to true only if the flaw cannot be fixed by revising the "
    "hypothesis."
)


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
    ReviewPerspective("methodology", _METHODOLOGY_PROMPT),
    ReviewPerspective("statistics", _STATISTICS_PROMPT),
)
"""视角顺序是稳定的：light_hard_gate 按此顺序扫描以确定 blocking_factor。"""


async def review_or_degrade(
    draft: IdeatorHypothesisDraft,
    idea_id: str,
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
        premises = (
            "\n".join(
                f"- [{item.role.value}] {item.claim} "
                f"(refs: {', '.join(item.supporting_refs) or '-'})"
                for item in draft.supported_premises
            )
            or "(no supported premises)"
        )
        prompt = "\n\n".join(
            [
                perspective.system_prompt,
                f"Review perspective: {perspective.perspective_id}\n"
                + _REVIEW_USER_PROMPT.format(
                    novel_hypothesis=draft.statement,
                    supported_premises=premises,
                    predicted_observations="\n".join(
                        f"- {item}" for item in draft.predicted_observations
                    ),
                    disconfirming_observations="\n".join(
                        f"- {item}" for item in draft.disconfirming_observations
                    ),
                ),
            ]
        )
        async with llm_sem if llm_sem is not None else nullcontext():
            judgment = await single_turn_structured_chat(
                prompt,
                SkepticJudgment,
                model=model,
                artifacts=artifacts,
            )
        return SkepticReport(
            idea_id=idea_id,
            perspective=perspective.perspective_id,
            critique=judgment.critique,
            unaddressed_risks=judgment.unaddressed_risks,
            fatal_flaw_found=judgment.fatal_flaw_found,
            input_ref=await artifacts.put_text(prompt),
        )
    except Exception as error:  # noqa: BLE001 - provider 报错形态不定，直接降级
        return SkepticReport(
            idea_id=idea_id,
            perspective=perspective.perspective_id,
            critique=f"review failed: {error}",
            unaddressed_risks=[],
            fatal_flaw_found=False,
            failed=True,
        )
