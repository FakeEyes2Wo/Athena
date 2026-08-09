"""项目 Composition Root 测试支持：带真实 model + fake 内层 LLM 的 make_project。"""

from pathlib import Path

from athena.agents.prompt_agent import load_prompt
from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent import settings
from athena.core.agent.provider import StreamEvent
from athena.core.agent.runtime import Agent
from athena.research.project_runtime import ProjectRuntime


class FakeProvider:
    """stub stream：直接 emit text_delta + response_completed，让内层 Agent 收尾。

    duck-typed ``ResponsesProvider``：不真正调 LLM（Task 6 起 data/init 内层
    LLM agent 的测试接缝）。
    """

    def __init__(self) -> None:
        self.model_name = "fake"

    async def stream(self, config, tools, messages, cancel, *, output_type=None):
        yield StreamEvent(
            kind="text_delta", data={"delta": "done", "accumulated": "done"}
        )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


def fake_inner_builder(agent_type, *, model, client, workspace):
    """fake 内层 agent 构建器：把 report.md + figures/*.png 预写进 workspace 再收尾。

    模拟真实 LLM 的产物（data_agent prompt 要求 workspace 根 report.md + 至少
    一张 ``figures/*`` 图），让外层编排（DataAgent 收集-提交）不依赖真实 API。
    """
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    figures = ws / "figures"
    figures.mkdir(exist_ok=True)
    (figures / "plot.png").write_bytes(b"fake-png")
    (ws / "report.md").write_text("分析报告", encoding="utf-8")
    return Agent(FakeProvider(), generic_tool_registry(ws), load_prompt(agent_type))


def make_project(tmp_path: Path) -> ProjectRuntime:
    """带 DeepSeek model + fake 内层 LLM 的组合根（LLM 相关测试的入口）。

    ``register_defaults`` 要求显式传 ``model``（无 model 报错）；测试统一经
    此 helper 用 ``settings.model_name()`` 装配，并注入 ``fake_inner_builder``
    让 data agent 的内层 LLM 用 fake provider 收尾（单测不 hit 真实 API）。
    """
    project = ProjectRuntime(tmp_path)
    project.register_defaults(
        model=settings.model_name(),
        inner_builder=fake_inner_builder,
    )
    return project
