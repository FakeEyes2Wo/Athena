"""策略并行多候选生成（步骤 [3]）与候选去重，取代 P0 的单策略生成。

原 Verbalized Sampling（一次 LLM 调用产出多个带自评概率的候选）已废弃：观察到的候选
同质化严重，mode collapse 没有被真正规避。改为 N 个独立策略 Agent（类比迁移/机制推演/
反直觉假设/约束松弛/边界外推）各自单轮产出一个候选、并行执行——呼应 review_board.py
三视角并行的先例，同样遵循 Occam's razor：策略之间只在系统 prompt 上有区别，都靠包内
信息（问题定格 + 已挖掘的文献空白）就能判定，不需要各自配检索工具。

去重是纯函数，按 novel_hypothesis 文本的词集 Jaccard 相似度判定，不接 LLM——多候选生成
阶段本身也不做自我批判（判定权仍在 gatekeeper），呼应 Co-Scientist"生成与审阅分离"的设计。
"""

import asyncio
import re
import uuid
from dataclasses import dataclass

from pydantic_ai.models import Model

from athena.utils.single_turn_chat import single_turn_chat
from athena.workflows.prompts import (
    GENERATION_STRATEGY_ANALOGICAL_TRANSFER_SYSTEM_PROMPT,
    GENERATION_STRATEGY_BOUNDARY_EXTRAPOLATION_SYSTEM_PROMPT,
    GENERATION_STRATEGY_CONSTRAINT_RELAXATION_SYSTEM_PROMPT,
    GENERATION_STRATEGY_COUNTERINTUITIVE_SYSTEM_PROMPT,
    GENERATION_STRATEGY_HEADER_TEMPLATE,
    GENERATION_STRATEGY_MECHANISTIC_REASONING_SYSTEM_PROMPT,
    GENERATION_STRATEGY_USER_PROMPT_TEMPLATE,
)
from athena.workflows.search.idea_schemas import (
    GapCandidate,
    HypothesisDraft,
    HypothesisPackage,
    ResearchProblemInput,
)


# ====== 策略定义 ======

@dataclass(frozen=True)
class GenerationStrategy:
    """一个生成策略：id 同时用作候选包 generation_strategy 字段的兜底值。

    Example:
        >>> GenerationStrategy("analogical_transfer", "system prompt").strategy_id
        'analogical_transfer'
    """
    strategy_id: str
    system_prompt: str


GENERATION_STRATEGIES: tuple[GenerationStrategy, ...] = (
    GenerationStrategy("analogical_transfer", GENERATION_STRATEGY_ANALOGICAL_TRANSFER_SYSTEM_PROMPT),
    GenerationStrategy("mechanistic_reasoning", GENERATION_STRATEGY_MECHANISTIC_REASONING_SYSTEM_PROMPT),
    GenerationStrategy("counterintuitive", GENERATION_STRATEGY_COUNTERINTUITIVE_SYSTEM_PROMPT),
    GenerationStrategy("constraint_relaxation", GENERATION_STRATEGY_CONSTRAINT_RELAXATION_SYSTEM_PROMPT),
    GenerationStrategy("boundary_extrapolation", GENERATION_STRATEGY_BOUNDARY_EXTRAPOLATION_SYSTEM_PROMPT),
)
"""顺序稳定：generate_candidates 按 sample_size 从头取前 N 个，同样输入下哪些策略参与是
确定的。五个策略与 MAX_VERBALIZED_SAMPLES 的数量对齐，保持 run_full_pipeline 现有调用点
传入的 sample_size 值（1-5）语义不变——不是巧合，是刻意让这次替换不动上层签名。"""


# ====== 常量 ======

MAX_VERBALIZED_SAMPLES: int = 5
"""生成侧并行策略数的上限，等于 len(GENERATION_STRATEGIES)。名字沿用旧的 Verbalized
Sampling 时代——虽然机制已经换成多 Agent 并行，但它作为"生成阶段最多产出几个候选"这个上界
的含义没变，改名要动全仓库十余处调用点，不在这轮范围内。"""
DEFAULT_DEDUP_SIMILARITY_THRESHOLD: float = 0.8
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


# ====== 单策略生成 ======

async def generate_one_strategy(
    problem: ResearchProblemInput,
    gaps: list[GapCandidate],
    strategy: GenerationStrategy,
    *,
    model: Model | str | None = None,
) -> HypothesisPackage:
    """跑一个策略 Agent，产出恰好一个候选包。单轮调用，不配检索工具——策略之间的差异
    只在系统 prompt，判定所需信息（问题定格 + 已挖掘空白）已经在参数里。

    idea_id 由代码分配，不向 LLM 索要，避免哈希碰撞或幻觉 id。sampling_probability 不要求
    模型自评——多策略并行下，不同策略各出一个候选，不再是"同一次采样内部比较概率"这件事，
    继续问模型要自评分反而是变相自证；HypothesisDraft.sampling_probability 默认 1.0，
    这里就用这个默认值，不覆盖。

    Example:
        >>> package = await generate_one_strategy(
        ...     problem, [], GENERATION_STRATEGIES[0], model=fake_model)  # doctest: +SKIP
        >>> package.generation_strategy
        'analogical_transfer'
    """
    gap_lines = "\n".join(f"- [{gap.gap_type}] {gap.description}" for gap in gaps) or "(no explicit gaps supplied)"
    evidence_lines = "\n".join(
        f"ev-{index}: {text}" for index, text in enumerate(problem.evidence_texts)
    ) or "(no evidence supplied)"
    constraint_lines = "\n".join(f"- {c}" for c in problem.constraints) or "(none)"

    user_prompt = GENERATION_STRATEGY_USER_PROMPT_TEMPLATE.format(
        question=problem.question, domain=problem.domain, objective=problem.objective,
        constraints=constraint_lines, evidence=evidence_lines, gaps=gap_lines,
    )
    header = GENERATION_STRATEGY_HEADER_TEMPLATE.format(strategy_id=strategy.strategy_id)
    prompt = f"{header}{strategy.system_prompt}\n\n{user_prompt}"

    draft = await single_turn_chat(prompt, HypothesisDraft, model=model)
    return HypothesisPackage(
        idea_id=f"idea-{uuid.uuid4().hex[:12]}",
        generation_strategy=draft.generation_strategy or strategy.strategy_id,
        novel_hypothesis=draft.statement,
        sampling_probability=draft.sampling_probability,
        supported_premises=draft.supported_premises,
        inference_chain=draft.inference_chain,
        predicted_observations=draft.predicted_observations,
        disconfirming_observations=draft.disconfirming_observations,
        validation_plan_ref=None,
        lineage_op="generate",
    )


# ====== 多候选生成（并行策略） ======

async def generate_candidates(
    problem: ResearchProblemInput,
    gaps: list[GapCandidate],
    *,
    sample_size: int = MAX_VERBALIZED_SAMPLES,
    model: Model | str | None = None,
) -> list[HypothesisPackage]:
    """并行跑 sample_size 个策略 Agent（GENERATION_STRATEGIES 里的前 sample_size 个），
    每个产出一个候选包。

    单个策略调用失败不拖垮整批——用 return_exceptions=True 收集，跳过失败的那个，返回
    其余成功的候选；全部失败则返回空列表（这是合法结果，不是错误：呼应旧版 Verbalized
    Sampling 里"candidates=[]"是合法输出的先例，图编排的 fan_out_candidates 已经处理了
    候选列表为空这条路径）。

    Example:
        >>> packages = await generate_candidates(problem, [], model=fake_model)  # doctest: +SKIP
        >>> len(packages) <= MAX_VERBALIZED_SAMPLES
        True
    """
    if not 1 <= sample_size <= MAX_VERBALIZED_SAMPLES:
        raise ValueError(f"sample_size must be within [1, {MAX_VERBALIZED_SAMPLES}], got {sample_size}")

    outcomes = await asyncio.gather(*[
        generate_one_strategy(problem, gaps, strategy, model=model)
        for strategy in GENERATION_STRATEGIES[:sample_size]
    ], return_exceptions=True)
    return [outcome for outcome in outcomes if isinstance(outcome, HypothesisPackage)]


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

    阈值原本是为"同一次调用产出的候选"标定的；多个独立策略 Agent 产出的文本分布可能不同
    （不同策略被设计成产出方向不同的假设），暂时沿用同一个阈值——没有实测证据支持调整到
    另一个数字，调整前应该先收集多策略场景下的实际相似度分布，而不是猜一个新阈值。

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
