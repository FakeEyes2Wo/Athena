"""TaskUnderstandAgent -- Kaggle competition agent entry point.

This Agent is the single entry point for the competition workflow. It holds a
ToolRegistry containing all competition-related tools and uses a ReAct loop to
autonomously decide tool-calling order, completing the full pipeline from
competition research through submission.
"""

from typing import TYPE_CHECKING

from athena.core.agent.agent import Agent, AgentConfig, create_agent
from athena.core.tool import ToolRegistry

# Search and understanding layer (Task 4)
from athena.tools.kaggle_search import (
    KaggleCompetitionSearchTool,
    KaggleDatasetDownloadTool,
    KaggleDiscussionSearchTool,
)

# Data acquisition layer -- HuggingFace datasets (Task 5)
from athena.tools.hf_dataset import HFDatasetDownloadTool, HFDatasetSearchTool

# Data acquisition layer -- HuggingFace models (Task 6)
from athena.tools.hf_model import HFModelDownloadTool, HFModelSearchTool

# Data preparation layer (Task 7)
from athena.tools.data_prepare import DataAnalyzeTool, DataCleanCodeGenTool

# Modeling and submission layer (Task 8)
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
    """Build a TaskUnderstandAgent with all competition tools registered.

    Args:
        model: LLM model name (e.g. "deepseek-v4-flash").
        client: OpenAI-compatible async client.
        max_turns: Maximum turns for the agent loop (competition workflows
                   need more turns).
        max_tokens: Maximum tokens per LLM request.
        temperature: LLM temperature parameter.

    Returns:
        A fully-configured Agent instance ready for ThreadRuntime.
    """
    # Build tool registry, sorted alphabetically for prompt-cache stability
    tools = ToolRegistry()

    # Search and understanding layer (3 tools)
    tools.register(KaggleCompetitionSearchTool())
    tools.register(KaggleDiscussionSearchTool())
    tools.register(KaggleDatasetDownloadTool())

    # Data acquisition layer -- HF datasets (2 tools)
    tools.register(HFDatasetSearchTool())
    tools.register(HFDatasetDownloadTool())

    # Data acquisition layer -- HF models (2 tools)
    tools.register(HFModelSearchTool())
    tools.register(HFModelDownloadTool())

    # Data preparation layer (2 tools)
    tools.register(DataAnalyzeTool())
    tools.register(DataCleanCodeGenTool())

    # Modeling and submission layer (4 tools)
    tools.register(SolutionDesignTool())
    tools.register(ProjectCodeGenTool())
    tools.register(CodeExecuteTool())
    tools.register(SubmissionBuildTool())

    # Guard: all 13 tools must be registered
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
