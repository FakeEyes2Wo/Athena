"""Kaggle Handoff Agent registration for Idea Generation.

SEARCH 的 Idea Generation 需要 Kaggle 社区证据（Discussion + Notebook）时，先由
这个 Agent 读取竞赛相关讨论和热门 notebook，把提炼结果写成 EDA 工作区里的
``KAGGLE_HANDOFF.md``。后续所有 Ideator Agent（gated / baseline / debate）都会
读取这份 handoff，作为假设生成的社区证据输入。

工具集由调用方注入：通常包含 ``kaggle_list_notebooks`` / ``kaggle_get_notebook`` /
``kaggle_list_discussions`` / ``kaggle_get_discussion``，以及 ``web_fetch`` 等回退
抓取工具。Agent 只写 ``KAGGLE_HANDOFF.md``，不修改 EDA 报告、baseline 或数据集。
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.core.tool import ToolRegistry
from athena.execution.runtime import ExecutionRuntime

KAGGLE_HANDOFF_AGENT_ID = "kaggle_handoff"
KAGGLE_HANDOFF_AGENT_TYPE = "kaggle_handoff"
KAGGLE_HANDOFF_FILENAME = "KAGGLE_HANDOFF.md"
KAGGLE_EVIDENCE_FILENAME = "KAGGLE_EVIDENCE.json"


class KaggleNotebookEvidence(BaseModel):
    """One notebook actually read by the handoff agent."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    version: str = ""
    title: str = ""
    url: str = ""

    @field_validator("ref", "version", "title", "url", mode="before")
    @classmethod
    def _coerce_to_str(cls, value: object) -> object:
        return str(value) if value is not None else ""


class KaggleDiscussionEvidence(BaseModel):
    """One discussion thread actually read by the handoff agent."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    title: str = ""
    url: str = ""

    @field_validator("ref", "title", "url", mode="before")
    @classmethod
    def _coerce_to_str(cls, value: object) -> object:
        return str(value) if value is not None else ""


class KaggleHandoffResult(BaseModel):
    """Structured output returned after writing the Kaggle handoff document."""

    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1)
    handoff_file: str = Field(default=KAGGLE_HANDOFF_FILENAME)
    notebooks: list[KaggleNotebookEvidence] = Field(
        default_factory=list,
        description="Notebooks actually read; include the exact version when known.",
    )
    discussions: list[KaggleDiscussionEvidence] = Field(
        default_factory=list,
        description="Discussion threads actually read.",
    )

    @field_validator("notebooks", "discussions", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return [] if value is None else value


def register_kaggle_handoff_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | None = None,
) -> None:
    """Register a fresh Kaggle Handoff Agent bound to the EDA workspace."""
    register_prompt_agent(
        registry,
        agent_type=KAGGLE_HANDOFF_AGENT_TYPE,
        output_type=KaggleHandoffResult,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
    )


__all__ = [
    "KAGGLE_HANDOFF_AGENT_ID",
    "KAGGLE_HANDOFF_AGENT_TYPE",
    "KAGGLE_HANDOFF_FILENAME",
    "KAGGLE_EVIDENCE_FILENAME",
    "KaggleDiscussionEvidence",
    "KaggleHandoffResult",
    "KaggleNotebookEvidence",
    "register_kaggle_handoff_agent",
]
