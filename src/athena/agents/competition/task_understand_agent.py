"""TaskUnderstandAgent —— Kaggle 竞赛智能体入口。

该 Agent 是竞赛流程的唯一入口。它持有所有竞赛相关工具的 ToolRegistry，
通过 ReAct loop 自主决策工具调用顺序，完成从搜索到提交的完整 pipeline。
"""

from typing import TYPE_CHECKING

from athena.core.agent.agent import Agent, AgentConfig, create_agent
from athena.core.tool import ToolRegistry

# ── MCP 接入层 ──
from athena.tools.mcp import register_mcp_tools
from athena.tools.mcp.config import McpServerConfig, load_mcp_servers

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
1. When you need external data or services (e.g. Kaggle, HuggingFace),
   FIRST search for available tools with mcp_search_tools, then call the
   discovered tool by its full name in a later turn.
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


async def build_task_understand_agent(
    model: str,
    client: "AsyncOpenAI | None" = None,
    *,
    work_root: str = "work",
    max_turns: int = 30,
    max_tokens: int = 8192,
    temperature: float = 0.1,
    mcp_servers: list[McpServerConfig] | None = None,
) -> Agent:
    """构建 TaskUnderstandAgent，注册全部原生工具与可选 MCP 工具。

    mcp_servers 为 None 时尝试从当前目录 mcp_servers.json 加载；
    未配置任何 server 时 MCP 接入层不生效。
    """
    # 构建工具注册表，按字母序排列以保证 prompt cache 稳定
    tools = ToolRegistry()

    # ── 数据获取层 —— HF 数据集 (2 工具) ──
    tools.register(HFDatasetSearchTool(work_root=work_root))
    tools.register(HFDatasetDownloadTool(work_root=work_root))

    # ── 数据获取层 —— HF 模型 (2 工具) ──
    tools.register(HFModelSearchTool(work_root=work_root))
    tools.register(HFModelDownloadTool(work_root=work_root))

    # ── 数据准备层 (2 工具) ──
    tools.register(DataAnalyzeTool(work_root=work_root))
    tools.register(DataCleanCodeGenTool(work_root=work_root))

    # ── 建模 & 提交层 (4 工具) ──
    tools.register(SolutionDesignTool(work_root=work_root))
    tools.register(ProjectCodeGenTool(work_root=work_root))
    tools.register(CodeExecuteTool(work_root=work_root))
    tools.register(SubmissionBuildTool(work_root=work_root))

    # Guard：原生工具数量固定；MCP 工具在下方动态追加，不参与此断言
    assert len(tools) == 10, f"Expected 10 native tools, got {len(tools)}"

    # ── MCP 接入层（可选）：配置驱动，懒连接；未配置 server 时完全惰性 ──
    servers = mcp_servers if mcp_servers is not None else load_mcp_servers()
    if servers:
        await register_mcp_tools(tools, servers, work_root=work_root)

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
