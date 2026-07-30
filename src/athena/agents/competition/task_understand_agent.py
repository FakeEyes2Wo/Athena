"""TaskUnderstandAgent —— Kaggle 竞赛智能体入口。

该 Agent 是竞赛流程的唯一入口。它持有所有竞赛相关工具的 ToolRegistry，
通过 ReAct loop 自主决策工具调用顺序，完成从搜索到提交的完整 pipeline。
"""

from typing import TYPE_CHECKING

from athena.core.agent.agent import Agent, AgentConfig, create_agent
from athena.core.tool import ToolRegistry

# ── 搜索 & 理解层 (Task 4) ──
from athena.tools.kaggle_search import (
    KaggleCompetitionSearchTool,
    KaggleDatasetDownloadTool,
    KaggleDiscussionSearchTool,
)

# ── 数据获取层 —— HuggingFace 数据集 (Task 5) ──
from athena.tools.hf_dataset import HFDatasetDownloadTool, HFDatasetSearchTool

# ── 数据获取层 —— HuggingFace 模型 (Task 6) ──
from athena.tools.hf_model import HFModelDownloadTool, HFModelSearchTool

# ── 数据准备层 (Task 7) ──
from athena.tools.data_prepare import DataAnalyzeTool, DataCleanCodeGenTool

# ── 建模 & 提交层 (Task 8) ──
from athena.tools.baseline_builder import (
    CodeExecuteTool,
    ProjectCodeGenTool,
    SolutionDesignTool,
    SubmissionBuildTool,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI

# TaskUnderstandAgent system prompt
_COMPETITION_SYSTEM_PROMPT = """\
You are TaskUnderstandAgent, an autonomous Kaggle competition agent.

## Your Goal
Given a competition name or URL, you autonomously complete the full
pipeline: research the competition -> acquire and augment data -> analyze
and clean data -> design a solution -> generate and execute code -> produce
a submission file.

## Decision-Making Rules
1. ALWAYS start by searching for competition info using kaggle_competition_search.
2. After understanding the task, search discussions/notebooks for solution ideas.
3. Download competition data, then search HuggingFace for augmentation datasets.
4. Analyze all data before cleaning. Generate and execute cleaning code.
5. Search for and download suitable pre-trained models if applicable.
6. Design a solution with a self-check rubric BEFORE generating code.
7. Generate project code, execute training, then inference.
8. If code execution fails, analyze the error log, fix the code, and re-run.
   Maximum 3 fix-retry cycles per failure.
9. Build the submission file in the required format.

## Artifact Tracking
After each tool call, note the returned artifact references. Use them as
inputs to subsequent tools. Do not lose track of references.

## Error Recovery
When a tool returns success=False, read the error message and decide:
- Retry with adjusted parameters
- Skip optional steps (e.g. augmentation if no datasets found)
- Report to user if a required step cannot complete

## Output
When done, summarize: what was built, key metrics, submission file location.
"""


def build_task_understand_agent(
    model: str,
    client: "AsyncOpenAI | None" = None,
    *,
    max_turns: int = 30,
    max_tokens: int = 8192,
    temperature: float = 0.1,
) -> Agent:
    """构建 TaskUnderstandAgent，注册所有竞赛工具。

    Args:
        model: LLM 模型名称（如 "deepseek-v4-flash"）。
        client: OpenAI 兼容的异步客户端。
        max_turns: Agent loop 最大轮次（竞赛流程需要较多轮次）。
        max_tokens: 每次 LLM 请求的最大 token 数。
        temperature: LLM 温度参数。

    Returns:
        配置完成的 Agent 实例，可直接用于 ThreadRuntime。
    """
    # 构建工具注册表，按字母序排列以保证 prompt cache 稳定
    tools = ToolRegistry()

    # ── 搜索 & 理解层 (3 工具) ──
    tools.register(KaggleCompetitionSearchTool())
    tools.register(KaggleDiscussionSearchTool())
    tools.register(KaggleDatasetDownloadTool())

    # ── 数据获取层 —— HF 数据集 (2 工具) ──
    tools.register(HFDatasetSearchTool())
    tools.register(HFDatasetDownloadTool())

    # ── 数据获取层 —— HF 模型 (2 工具) ──
    tools.register(HFModelSearchTool())
    tools.register(HFModelDownloadTool())

    # ── 数据准备层 (2 工具) ──
    tools.register(DataAnalyzeTool())
    tools.register(DataCleanCodeGenTool())

    # ── 建模 & 提交层 (4 工具) ──
    tools.register(SolutionDesignTool())
    tools.register(ProjectCodeGenTool())
    tools.register(CodeExecuteTool())
    tools.register(SubmissionBuildTool())

    # 守卫：确保全部 13 个工具已注册
    assert len(tools) == 13, (
        f"Expected 13 competition tools, got {len(tools)}"
    )

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=_COMPETITION_SYSTEM_PROMPT,
        client=client,
        max_turns=max_turns,
        max_tokens=max_tokens,
        temperature=temperature,
        name="TaskUnderstandAgent",
        description=(
            "Autonomous Kaggle competition agent that researches, "
            "downloads data, builds baselines, and produces submissions."
        ),
    )
