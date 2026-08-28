"""Citation verification mixin for ``AgentTurnRunner``.

Keeps the two-layer citation check (did the lane read it? does the passage
support the claim?) in one focused module instead of bloating the runner.
"""

import asyncio
from typing import Any

from athena.core.research_models import Hypothesis
from athena.research.idea_generation.citation_support import (
    SUPPORT_PROMPT,
    SupportVerdict,
    format_evidence,
    parse_verdict,
)
from athena.utils.single_turn_chat import single_turn_chat


class SupportVerificationMixin:
    """Mixin providing source and support verification for hypotheses."""

    _runtime: Any

    async def _verify_sources(self, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        """只保留本轮**真正打开过正文**的论文，其余引用一律丢弃。

        ``ranker.rubric_prior`` 给"有引用"加 0.3（在总分里占 0.12），也就是说凭空写一个
        paper id 就能让候选往前排。这条奖励只有在引用可核验时才成立，否则它奖励的是幻觉。
        校验必须在入图之前做——进了图就是排序的输入了。

        **判据是"读过"，不是"在语料里"。** 第一版只查 id 是否存在于语料，真机（2026-08-16
        第 12 次）证明那太松：Ideator 拿《数据增强综述》支持"两两交互特征"、拿《信用卡欺诈
        检测综述》同时支持 target encoding 与 SMOTE，而语料里三篇真正讲 AUC 的论文一次都
        没被引用。带引用和不带引用的假设提的是同一批干预——引用是事后贴的标签，不是想法的
        来源。这些论文都在检索结果里出现过，只是从没被 ``paper_chunk_read`` 打开，所以
        "读过"能拦住而"存在"拦不住。

        没有语料时整段跳过：此时 ``sources`` 按 schema 本就该为空，不该顺手清掉别的来源
        写进去的内容。
        """
        rt = self._runtime
        if not await rt.corpus_paper_ids():
            return hypotheses
        opened = rt.corpus_papers_read()
        verified: list[Hypothesis] = []
        dropped = 0
        for hypothesis in hypotheses:
            kept = [source for source in hypothesis.sources if source in opened]
            dropped += len(hypothesis.sources) - len(kept)
            verified.append(hypothesis.model_copy(update={"sources": kept}))
        if dropped:
            await rt.publish_output(
                source="agent",
                channel="error",
                text=(
                    f"dropped {dropped} citation(s) to papers this round never opened; "
                    "cite only what you read with paper_chunk_read — a paper that "
                    "merely appeared in search results is not evidence"
                ),
            )
        return await self._verify_support(verified)

    async def _verify_support(self, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        """再问一层：被引的那几段正文，到底支不支持这条主张。

        ``_verify_sources`` 查的是**行为**（读没读过），这一层查的是**内容**。两者都需要：

        判定失败（模型不可用、解析不出来）时**保留原引用**：这一层是增益，不该因为一次
        端点抖动就把整轮的引用清空。丢弃与保留的方向在这里是相反的——解析不出来判"不支持"
        是单条判定内部的保守，整层不可用则不该改变已经通过前一关的结论。
        """
        rt = self._runtime
        if rt.model is None or not any(item.sources for item in hypotheses):
            return hypotheses
        passages = await rt.corpus_passages_read()
        checked: list[Hypothesis] = []
        rejected: list[str] = []
        for hypothesis in hypotheses:
            if not hypothesis.sources:
                checked.append(hypothesis)
                continue
            verdicts = await asyncio.gather(
                *(
                    self._support_verdict(hypothesis, source, passages.get(source, []))
                    for source in hypothesis.sources
                ),
                return_exceptions=True,
            )
            kept: list[str] = []
            for source, verdict in zip(hypothesis.sources, verdicts):
                if isinstance(verdict, BaseException):
                    # 整层不可用 → 保留，别让端点抖动清空引用
                    kept.append(source)
                    continue
                if verdict.supports:
                    kept.append(source)
                else:
                    rejected.append(f"{source} ({verdict.why or 'no support'})")
            checked.append(hypothesis.model_copy(update={"sources": kept}))
        if rejected:
            await rt.publish_output(
                source="agent",
                channel="error",
                text=(
                    f"dropped {len(rejected)} citation(s) whose passages do not "
                    f"support the claim: {'; '.join(rejected[:5])}"
                ),
            )
        return checked

    async def _support_verdict(
        self, hypothesis: Hypothesis, paper_id: str, passages: list[str]
    ) -> SupportVerdict:
        """问一次"这段话支持这条主张吗"，返回结构化判定。"""
        rt = self._runtime
        prompt = SUPPORT_PROMPT.format(
            claim=hypothesis.statement,
            intervention=hypothesis.intervention,
            paper_id=paper_id,
            evidence=format_evidence(passages),
        )
        content = await single_turn_chat(
            prompt, model=rt.model, client=rt.client, max_tokens=200
        )
        return parse_verdict(paper_id, content)
