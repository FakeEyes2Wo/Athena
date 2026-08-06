"""TaskUnderstandAgent —— 通用任务理解与执行智能体入口。

该 Agent 是任务执行流程的唯一入口。它持有所有任务相关工具的 ToolRegistry，
通过 ReAct loop 自主决策工具调用顺序，完成从理解需求到产出交付的完整 pipeline。
"""

from typing import TYPE_CHECKING

from athena.core.agent.agent import Agent, AgentConfig, create_agent
from athena.core.tool import ToolRegistry
from athena.prompts import load_prompt

# ── MCP 接入层 ──
from athena.tools.mcp import register_mcp_tools
from athena.tools.mcp.config import McpServerConfig, load_mcp_servers

# ── 数据获取层 —— HuggingFace 数据集 (Task 5) ──
from athena.tools.hf_dataset import HFDatasetDownloadTool, HFDatasetSearchTool

# ── 数据获取层 —— HuggingFace 模型 (Task 6) ──
from athena.tools.hf_model import HFModelDownloadTool, HFModelSearchTool

# ── 数据准备层 (Task 7) ──
from athena.tools.data_prepare import DataAnalyzeTool, DataCleanCodeGenTool

if TYPE_CHECKING:
    from openai import AsyncOpenAI



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

    # Guard：原生工具数量固定；MCP 工具在下方动态追加，不参与此断言
    assert len(tools) == 6, f"Expected 6 native tools, got {len(tools)}"

    # ── MCP 接入层（可选）：配置驱动，懒连接；未配置 server 时完全惰性 ──
    servers = mcp_servers if mcp_servers is not None else load_mcp_servers()
    if servers:
        await register_mcp_tools(tools, servers, work_root=work_root)

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=load_prompt("competition/task_understand_system.txt"),
        client=client,
        max_turns=max_turns,
        max_tokens=max_tokens,
        temperature=temperature,
        name="TaskUnderstandAgent",
        description=(
            "Autonomous task execution agent that researches, "
            "downloads data, builds baselines, and produces deliverables."
        ),
    )
