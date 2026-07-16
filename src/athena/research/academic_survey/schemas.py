"""AcademicSurvey（SPAR 式检索回路）的数据契约。

设计对应主设计文档"基础设施 -> AcademicSurvey"一节：输入研究主题与约束，
输出去重、排序、带逐项判定证据与全文 markdown 的 ``SurveyCorpus``。全文与
逐章节文本等大对象一律保存为 ``ArtifactRef``；相关性判定必须绑定版本化
rubric 并保留逐项证据，不允许只返回总分。
"""

from typing import Literal

from pydantic import BaseModel, Field

from athena.core.schemas import ArtifactRef


class SurveyRequest(BaseModel):
    """一次文献调研的输入单。"""

    topic: str = Field(description="Research topic in natural language.")
    constraints: list[str] = Field(
        default_factory=list,
        description="Hard filters such as year range, venues, or languages.",
    )
    mode: Literal["fast", "diligent"] = Field(
        description="Budget preset controlling rounds, channel fan-out and RefChain depth."
    )


class CriterionJudgment(BaseModel):
    """单条可独立核验判据的判定结果。"""

    criterion: str = Field(description="One independently verifiable relevance criterion.")
    verdict: Literal["met", "unmet", "unknown"]
    evidence: str = Field(description="Evidence quoted from title or abstract.")


class RelevanceJudgment(BaseModel):
    """一篇论文的分解式相关性判定，绑定版本化 rubric。"""

    rubric_version: str = Field(description="Version of the relevance rubric used.")
    criteria: list[CriterionJudgment] = Field(
        description="Per-criterion evidence; never a bare score."
    )
    verdict: Literal["relevant", "irrelevant", "uncertain"]


class PaperContent(BaseModel):
    """markitdown 转换出的结构化全文；按源文件指纹全局缓存。"""

    fingerprint: str = Field(
        description="Source-file hash; conversions are cached globally by this key."
    )
    converter: str = Field(description="Converter name and version, e.g. 'markitdown-x.y'.")
    markdown_ref: ArtifactRef = Field(description="Full-paper markdown.")
    sections: dict[str, ArtifactRef] = Field(
        description="Heading path -> section markdown; direct input for RAG chunking."
    )


class PaperRecord(BaseModel):
    """一篇候选论文的最小档案；relevant 论文在交给 RAG 前必须填 ``content``。"""

    paper_id: str = Field(description="Canonical id: DOI, else arXiv id, else S2 corpus id.")
    title: str
    abstract: str
    meta: dict = Field(default_factory=dict, description="Year, venue, citation count, etc.")
    judgment: RelevanceJudgment
    rank_score: float = Field(description="Fused relevance x authority x recency score.")
    content: PaperContent | None = Field(
        default=None,
        description="Filled only for relevant papers; required before hand-off to RAG.",
    )


class SurveyCorpus(BaseModel):
    """检索回路的最终产物，也是下游 Paper Reading RAG 的唯一入库来源。"""

    request_ref: ArtifactRef
    prompt_bundle_version: str = Field(
        description="PromptBundle version that produced this corpus."
    )
    papers: list[PaperRecord]
    stats_ref: ArtifactRef = Field(
        description="Rounds and saturation curve for stop diagnostics."
    )


class PromptBundle(BaseModel):
    """回路中各 LLM 模块的提示词集合，版本化保存为 artifact，供 GEPA 离线优化。"""

    version: str = Field(description="Bundle version recorded into every SurveyCorpus.")
    prompts: dict[str, str] = Field(
        description="Module name -> English prompt template, e.g. 'understand', 'judge'."
    )
