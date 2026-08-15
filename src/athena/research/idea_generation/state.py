"""运行时依赖（Idea Generation 流水线共用）。

旧分支这里还有 langgraph 的 PipelineState/CandidateState（服务 checkpoint 序列化边界）——
本分支不引入 langgraph 依赖（见 __init__.py 模块docstring），编排改为纯 asyncio（见
workflow.py），所以这里只保留"一次 Run 的运行时依赖"这一半，不再需要可序列化状态与不可
序列化依赖的拆分。
"""

import asyncio
from dataclasses import dataclass

from athena.core.agent import Agent
from athena.core.contracts import ArtifactRef, ArtifactStore


@dataclass(frozen=True)
class PipelineDeps:
    """一次 Run 的运行时依赖。

    两个信号量：LLM 16 / retrieval 4（成本特性差一个量级），共用名额池会让慢的检索循环占满
    名额、把快的单轮调用堵在后面。

    Example:
        >>> deps = PipelineDeps(artifacts=store, corpus_ref=ref, llm_sem=s1, retrieval_sem=s2,
        ...     gap_miner_agent=a1, novelty_agent=a2, domain_review_agent=a3, model="m")  # doctest: +SKIP
    """
    artifacts: ArtifactStore
    corpus_ref: ArtifactRef
    llm_sem: asyncio.Semaphore
    retrieval_sem: asyncio.Semaphore
    gap_miner_agent: Agent
    novelty_agent: Agent
    domain_review_agent: Agent
    model: str
