"""REVISE 修订闭环（步骤 [8] 之后）：被 hard_gate 判 REVISE 的候选与拦住它的那个审阅视角
进行有界辩论，修订后按逐报告输入指纹只重跑真正失效的证据，再交回 gatekeeper 终审。

三条贯穿本模块的约束：
- **不产出 verdict**。终止条件用 gatekeeper 已有的 rubric 项谓词 perspective_ok（返回 bool），
  判决权全程留在 hard_gate 手里，整个闭环只调用它两次（进入前、终审）。
- **辩论轮不跑检索**。domain_consistency 的检索转录已经冻结在 SkepticReport.transcript_ref
  里，对修订稿重新表态只需一次 single_turn_chat。检索只可能出现在终局 staleness 刷新里。
- **reviser 看不到 sampling_probability**。它写的 rebuttal 要送进审阅侧 prompt，看得见就可能
  经由答辩文本泄漏（Co-Scientist：审阅侧不得锚定生成侧自评）。prompt 逐字段拼装，禁止
  model_dump_json()。
"""

import asyncio

from pydantic_ai.models import Model

from athena.core.schemas import ArtifactRef
from athena.storage.artifact_store import ArtifactStore
from athena.utils.single_turn_chat import single_turn_chat
from athena.workflows.prompts import (
    REVISER_SYSTEM_PROMPT,
    REVISION_USER_PROMPT_TEMPLATE,
)
from athena.workflows.search.evidence_retrieval import build_novelty_question, limited_by
from athena.workflows.search.idea_schemas import (
    HypothesisPackage,
    NoveltyEvidenceReport,
    RevisionDraft,
    SkepticReport,
)
from athena.workflows.search.review_board import (
    REVIEW_PERSPECTIVES,
    build_perspective_input,
    format_premise_lines,
)


# ====== 常量 ======

MAX_DEBATE_ROUNDS: int = 2
"""辩论轮次上限。1 轮退化成单向修订——对手对修订稿的表态即终审，没有往返，不成其为辩论；
2 轮是"对手对修订稿提新意见 → reviser 再应"这一往返成立的最小轮数。单候选最坏 +4 次
single_turn_chat。**无经验依据**，与 MAX_TOLERATED_RISKS / MAX_TOTAL_RISKS 同属"先取保守
起点、真实跑过几轮后一并校准"。"""

MAX_REVISION_ATTEMPTS: int = 2
"""单次 reviser 调用的最大尝试次数（即重试 1 次）。沿用 MAX_GENERATION_ATTEMPTS /
MAX_REVIEW_ATTEMPTS 的先例。重试逻辑在核心流程跑通之后才接进辩论循环（见 Task 10）。"""


# ====== Reviser（生成侧：看得到 critique，看不到阈值与自评概率） ======

def build_revision_prompt(
    package: HypothesisPackage,
    *,
    blocking_factor: str,
    debated_perspective: str,
    reviews: list[SkepticReport],
    prior_rounds: list[str],
) -> str:
    """拼装 reviser 的 prompt。逐字段拼装，**绝不序列化整个 package**——model_dump_json()
    会一次性带出 sampling_probability，把三道泄漏防线同时废掉。

    刻意把另外两个视角的 critique 也给出来：它们的报告在辩论期间被冻结复用，reviser 看不见
    就只能瞎改，而改坏了要到终审才暴露、还会触发 staleness 重跑（最贵的那条路径）。

    Example:
        >>> "risk_ok_methodology" in build_revision_prompt(package,
        ...     blocking_factor="risk_ok_methodology", debated_perspective="methodology",
        ...     reviews=reviews, prior_rounds=[])  # doctest: +SKIP
        True
    """
    by_perspective = {r.perspective: r for r in reviews}
    blocking = by_perspective.get(debated_perspective)
    premise_lines = format_premise_lines(package)
    other_lines = "\n".join(
        f"- [{r.perspective}] {r.critique} | risks: {r.unaddressed_risks}"
        for r in reviews if r.perspective != debated_perspective
    ) or "(none)"

    user_prompt = REVISION_USER_PROMPT_TEMPLATE.format(
        blocking_factor=blocking_factor,
        debated_perspective=debated_perspective,
        blocking_critique=blocking.critique if blocking else "(unavailable)",
        blocking_risks="\n".join(f"- {r}" for r in blocking.unaddressed_risks)
                       if blocking and blocking.unaddressed_risks else "(none listed)",
        other_critiques=other_lines,
        novel_hypothesis=package.novel_hypothesis,
        supported_premises=premise_lines,
        predicted_observations="\n".join(f"- {o}" for o in package.predicted_observations),
        disconfirming_observations="\n".join(
            f"- {o}" for o in package.disconfirming_observations),
        prior_rounds="\n".join(f"- {r}" for r in prior_rounds) or "(this is the first round)",
    )
    return f"{REVISER_SYSTEM_PROMPT}\n\n{user_prompt}"


async def revise_candidate(
    package: HypothesisPackage,
    *,
    blocking_factor: str,
    debated_perspective: str,
    reviews: list[SkepticReport],
    prior_rounds: list[str],
    llm_sem: asyncio.Semaphore | None = None,
    model: Model | str | None = None,
) -> tuple[HypothesisPackage, RevisionDraft]:
    """跑一次修订，返回 (修订稿, RevisionDraft)。

    idea_id / sampling_probability / revision_round / lineage_op 全部由代码填，不向 LLM 索要：
    idea_id 必须保持不变（hard_gate 强制全套报告同 id），sampling_probability 原样搬运原候选的
    自评（reviser 无权给自己抬分）。

    修订内容构造不出合法 HypothesisPackage 时**向外抛出**（校验器要求 predictions 与
    disconfirmers 均非空），由调用方按 reviser 失败处理——绝不放宽校验器。

    Example:
        >>> revised, draft = await revise_candidate(package,
        ...     blocking_factor="risk_ok_methodology", debated_perspective="methodology",
        ...     reviews=reviews, prior_rounds=[])  # doctest: +SKIP
        >>> revised.lineage_op
        'revise'
    """
    prompt = build_revision_prompt(
        package, blocking_factor=blocking_factor, debated_perspective=debated_perspective,
        reviews=reviews, prior_rounds=prior_rounds,
    )
    async with limited_by(llm_sem):
        draft = await single_turn_chat(prompt, RevisionDraft, model=model)

    revised = HypothesisPackage(
        idea_id=package.idea_id,
        generation_strategy=package.generation_strategy,
        sampling_probability=package.sampling_probability,
        novel_hypothesis=draft.revised_novel_hypothesis,
        supported_premises=draft.revised_premises,
        inference_chain=package.inference_chain,
        predicted_observations=draft.revised_predicted_observations,
        disconfirming_observations=draft.revised_disconfirming_observations,
        validation_plan_ref=None,
        revision_round=package.revision_round + 1,
        lineage_op="revise",
    )
    return revised, draft


# ====== staleness：逐报告的输入指纹比对 ======

async def novelty_is_stale(
    package: HypothesisPackage,
    novelty: NoveltyEvidenceReport,
    *,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
) -> bool:
    """novelty 报告的输入是否已失效。用当前 package 重建检索提问、落盘取内容寻址 ref，与报告
    存档的 input_ref 比对——ArtifactStore 是 sha256 内容寻址，ref 相等即内容相等。

    input_ref 为 None（失败降级报告）一律判 stale：无法证明未失效就不能复用。

    Example:
        >>> await novelty_is_stale(package, novelty, artifacts=store,
        ...     corpus_ref=corpus_ref)  # doctest: +SKIP
        False
    """
    if novelty.input_ref is None:
        return True
    current = await artifacts.put_text(build_novelty_question(package, corpus_ref))
    return novelty.input_ref != current


async def stale_perspectives(
    package: HypothesisPackage,
    reviews: list[SkepticReport],
    *,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    prior_transcript: str,
) -> set[str]:
    """返回输入已失效、需要重跑的视角 id 集合。

    **prior_transcript 必须是刷新后当前有效的那份转录**：domain_consistency 的输入模板内嵌
    prior_retrieval，存档指纹编码的是旧转录，传入新转录才能让级联（novelty 重跑 →
    domain_consistency 也失效）自动成立。调用方因此必须按"先 novelty、再视角"的顺序调。

    Example:
        >>> await stale_perspectives(package, reviews, artifacts=store,
        ...     corpus_ref=corpus_ref, prior_transcript="")  # doctest: +SKIP
        set()
    """
    by_perspective = {r.perspective: r for r in reviews}
    stale: set[str] = set()
    for perspective in REVIEW_PERSPECTIVES:
        report = by_perspective.get(perspective.perspective_id)
        if report is None or report.input_ref is None:
            stale.add(perspective.perspective_id)
            continue
        current = await artifacts.put_text(build_perspective_input(
            package, perspective, corpus_ref=corpus_ref, prior_transcript=prior_transcript))
        if report.input_ref != current:
            stale.add(perspective.perspective_id)
    return stale
