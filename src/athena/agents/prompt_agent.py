"""prompt 驱动 agent 构建：md prompt + 通用工具 → 内层 LLM ReAct Agent。

业务 Agent 的两层结构（设计 prompt-driven-agents §2）：
- 内层：``Agent``（ReAct 循环）+ ``ResponsesProvider`` + ``ToolRegistry``
  （通用工具，沙箱限定 workspace）+ system prompt（``core/agent/prompts/*.md``）。
- 外层：Python 编排器（data/init/report）收集产物、提交 Bundle/Artifact。

本模块只做内层构建：``load_prompt`` 读取固定格式 md；``build_llm_agent`` 把
provider + 通用工具 + prompt 组装成 ``Agent``。
"""

from pathlib import Path

from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.runtime import Agent
from athena.core.tool import ToolRegistry

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "core" / "agent" / "prompts"


def load_prompt(agent_type: str) -> str:
    """读取 core/agent/prompts/{agent_type}_agent.md；缺失直接报错。

    提示文件按 ``{agent_type}_agent.md`` 命名（如 ``data_agent.md``）；先按
    ``_agent`` 后缀直查（所有真实提示都用该命名），再回退到 ``{agent_type}.md``
    ，兼容两种命名。
    """
    path = _PROMPT_DIR / f"{agent_type}_agent.md"
    if not path.is_file():
        path = _PROMPT_DIR / f"{agent_type}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt not found for agent_type: {agent_type}")
    return path.read_text(encoding="utf-8")


def build_llm_agent(
    agent_type: str,
    *,
    model: str,
    client,
    workspace: Path,
    extra_tools: ToolRegistry | None = None,
) -> Agent:
    """构造 ReAct LLM agent：system_prompt = prompt 文件，tools = 通用工具。

    ``extra_tools`` 可选追加业务工具（ToolRegistry 不支持批量 merge，逐个
    register）；本任务 data/init/report 只依赖通用工具，不传。
    """
    tools = generic_tool_registry(workspace)
    if extra_tools is not None:
        for spec in extra_tools.specs:
            tools.register(extra_tools.resolve(spec.name))
    return Agent(
        ResponsesProvider(model, client=client),
        tools,
        load_prompt(agent_type),
    )
