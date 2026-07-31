"""Verbalized Sampling 多候选生成（步骤 [3]）与候选去重，取代 P0 的单策略生成。

Verbalized Sampling：一次 LLM 调用产出多个带自评概率的候选，规避 mode collapse（论文原文，
见设计文档第5节引用表）。去重是纯函数，按 novel_hypothesis 文本的词集 Jaccard 相似度判定，
不接 LLM——多候选生成阶段本身也不做自我批判（判定权仍在 gatekeeper），呼应 Co-Scientist
"生成与审阅分离"的设计。
"""

import re
import uuid

from pydantic_ai.models import Model

from athena.utils.single_turn_chat import single_turn_chat
from athena.workflows.prompts import (
    VERBALIZED_SAMPLING_SYSTEM_PROMPT,
    VERBALIZED_SAMPLING_USER_PROMPT_TEMPLATE,
)
from athena.workflows.search.idea_schemas import (
    GapCandidate,
    HypothesisPackage,
    ResearchProblemInput,
    VerbalizedSamplingResponse,
)


# ====== 常量 ======

VERBALIZED_SAMPLING_STRATEGY: str = "verbalized_sampling_v1"
MAX_VERBALIZED_SAMPLES: int = 5
DEFAULT_DEDUP_SIMILARITY_THRESHOLD: float = 0.8
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


# ====== 多候选生成 ======

async def generate_candidates(
    problem: ResearchProblemInput,
    gaps: list[GapCandidate],
    *,
    sample_size: int = MAX_VERBALIZED_SAMPLES,
    model: Model | str | None = None,
) -> list[HypothesisPackage]:
    """Verbalized Sampling：一次 LLM 调用产出 sample_size 个带自评概率的候选包。

    idea_id 由代码分配，不向 LLM 索要，避免哈希碰撞或幻觉 id。

    Example:
        >>> packages = await generate_candidates(problem, [], model=fake_model)  # doctest: +SKIP
        >>> len(packages) <= MAX_VERBALIZED_SAMPLES
        True
    """
    if not 1 <= sample_size <= MAX_VERBALIZED_SAMPLES:
        raise ValueError(f"sample_size must be within [1, {MAX_VERBALIZED_SAMPLES}], got {sample_size}")

    gap_lines = "\n".join(f"- [{gap.gap_type}] {gap.description}" for gap in gaps) or "(no explicit gaps supplied)"
    evidence_lines = "\n".join(
        f"ev-{index}: {text}" for index, text in enumerate(problem.evidence_texts)
    ) or "(no evidence supplied)"
    constraint_lines = "\n".join(f"- {c}" for c in problem.constraints) or "(none)"

    user_prompt = VERBALIZED_SAMPLING_USER_PROMPT_TEMPLATE.format(
        question=problem.question, domain=problem.domain, objective=problem.objective,
        constraints=constraint_lines, evidence=evidence_lines, gaps=gap_lines, sample_size=sample_size,
    )
    prompt = f"{VERBALIZED_SAMPLING_SYSTEM_PROMPT}\n\n{user_prompt}"

    response = await single_turn_chat(prompt, VerbalizedSamplingResponse, model=model)

    packages: list[HypothesisPackage] = []
    for draft in response.candidates[:sample_size]:
        packages.append(HypothesisPackage(
            idea_id=f"idea-{uuid.uuid4().hex[:12]}",
            generation_strategy=draft.generation_strategy or VERBALIZED_SAMPLING_STRATEGY,
            novel_hypothesis=draft.statement,
            sampling_probability=draft.sampling_probability,
            supported_premises=draft.supported_premises,
            inference_chain=draft.inference_chain,
            predicted_observations=draft.predicted_observations,
            disconfirming_observations=draft.disconfirming_observations,
            validation_plan_ref=None,
            lineage_op="generate",
        ))
    return packages


# ====== 候选去重 ======

def _tokenize(text: str) -> set[str]:
    """把文本归一化成小写字母数字词元集合，用于 Jaccard 相似度比较。

    Example:
        >>> sorted(_tokenize("X causes Y!"))
        ['causes', 'x', 'y']
    """
    return set(_TOKEN_PATTERN.findall(text.lower()))


def _jaccard_similarity(tokens_a: set[str], tokens_b: set[str]) -> float:
    """计算两个词元集合的 Jaccard 相似度；两边都空视为完全相同（避免除零）。

    Example:
        >>> _jaccard_similarity({"x", "y"}, {"x", "z"})
        0.3333333333333333
    """
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def deduplicate_candidates(
    packages: list[HypothesisPackage],
    *,
    similarity_threshold: float = DEFAULT_DEDUP_SIMILARITY_THRESHOLD,
) -> list[HypothesisPackage]:
    """按 novel_hypothesis 文本的词集 Jaccard 相似度去重，同组内保留 sampling_probability
    最高的一个候选；纯函数，不接 LLM。

    Example:
        >>> a = HypothesisPackage(idea_id="a", generation_strategy="s", novel_hypothesis="X causes Y",
        ...     sampling_probability=0.9, supported_premises=[], inference_chain=[],
        ...     predicted_observations=["p"], disconfirming_observations=["d"], lineage_op="generate")
        >>> len(deduplicate_candidates([a, a]))
        1
    """
    kept: list[HypothesisPackage] = []
    kept_tokens: list[set[str]] = []
    for package in sorted(packages, key=lambda p: p.sampling_probability, reverse=True):
        tokens = _tokenize(package.novel_hypothesis)
        if any(_jaccard_similarity(tokens, existing) >= similarity_threshold for existing in kept_tokens):
            continue
        kept.append(package)
        kept_tokens.append(tokens)
    return kept
