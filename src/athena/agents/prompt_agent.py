"""prompt 驱动 agent 构建：md prompt + 通用工具 → 内层 LLM ReAct Agent。

业务 Agent 的两层结构（设计 prompt-driven-agents §2）：
- 内层：``Agent``（ReAct 循环）+ ``ResponsesProvider`` + ``ToolRegistry``
  （通用工具，沙箱限定 workspace）+ system prompt（``agents/prompts/*.md``）。
- 外层：Python 编排器（data/init/report）收集产物、提交 Bundle/Artifact。

本模块只做内层构建：``load_prompt`` 读取固定格式 md；``build_llm_agent`` 把
provider + 通用工具 + prompt 组装成 ``Agent``；``register_prompt_agent`` 统一
各 ``register_*_agent`` 的工厂共性。
"""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent.models import AgentConfig
from athena.core.agent.provider import create_provider
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentSpec, JsonCodec
from athena.core.contracts import ArtifactStore
from athena.core.tool import ToolRegistry

if TYPE_CHECKING:
    from athena.execution.runtime import ExecutionRuntime

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(agent_type: str) -> str:
    """读取 agents/prompts/{agent_type}_agent.md；缺失直接报错。

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
    runtime: "ExecutionRuntime | None" = None,
) -> Agent:
    """构造 ReAct LLM agent：system_prompt = prompt 文件，tools = 通用工具。

    ``extra_tools`` 可选追加业务工具（ToolRegistry 不支持批量 merge，逐个
    register）。``runtime`` 提供时注册 ``shell_command`` 工具，并把简洁运行时
    摘要注入 system prompt 开头（shared-execution-runtime-design §Agent Context）。
    """
    tools = generic_tool_registry(workspace, runtime=runtime)
    if extra_tools is not None:
        for spec in extra_tools.specs:
            tools.register(extra_tools.resolve(spec.name))
    prompt = load_prompt(agent_type)
    if runtime is not None:
        prompt = runtime.runtime_summary(workspace) + "\n\n" + prompt
    return Agent(
        create_provider(model, client=client),
        tools,
        prompt,
    )


def register_prompt_agent(
    registry: AgentTypeRegistry,
    *,
    agent_type: str,
    output_type: type,
    workspace: Path | Callable[[str], Path],
    runtime: "ExecutionRuntime",
    provider: object,
    artifacts: ArtifactStore,
    name: str | None = None,
    extra_tools: ToolRegistry | Callable[[], ToolRegistry | None] | None = None,
    prompt_agent_type: str | None = None,
) -> None:
    """注册 prompt-driven ReAct Agent 工厂（各 ``register_*_agent`` 的共性）。

    ``workspace`` 可为固定 ``Path`` 或 ``Callable[[agent_id], Path]``（plan 按
    每个 hypothesis 动态解析工作区）；``name`` 支持 ``{agent_id}`` 占位符，缺省
    为 ``f"{agent_type}-agent"``。max_turns 统一用 ``AgentConfig`` 默认 200。

    ``extra_tools`` 可选追加业务工具（如 Kaggle 工具）：``ToolRegistry`` 不支持
    批量 merge，逐个 register。也可以是零参 callable，在 factory 创建实例时惰性
    求值——用于工具是否可用取决于运行时状态（如 Supervisor 是否接入 Kaggle）的
    场景。

    ``prompt_agent_type`` 让 prompt 文件名与注册名解耦，缺省二者相同。ideator 的
    消融开关要在同一个注册名下切换两份不同契约的 prompt（``ideator_agent.md`` 与
    ``ideator_gated_agent.md``），是目前唯一的用例。
    """

    def factory(agent_id: str) -> AgentSpec:
        root = workspace(agent_id) if callable(workspace) else workspace
        tools = generic_tool_registry(root, runtime=runtime)
        resolved = extra_tools() if callable(extra_tools) else extra_tools
        if resolved is not None:
            for spec in resolved.specs:
                tools.register(resolved.resolve(spec.name))
        agent = Agent(
            provider,
            tools,
            load_prompt(prompt_agent_type or agent_type),
            AgentConfig(name=(name or f"{agent_type}-agent").format(agent_id=agent_id)),
            output_type=output_type,
            artifacts=artifacts,
        )
        return AgentSpec(
            runner=BaseAgentRunner(agent, tools=tools, agent_type=agent_type),
            codec=JsonCodec(),
        )

    registry.register(agent_type, factory)
