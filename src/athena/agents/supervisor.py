"""root SupervisorAgent（设计 supervisor-agent-design §3、§5）。

动态编排项目内已注册 Agent；只通过 spawn/send/followup/wait_for/wait_for_human
五个受控命令决策（经 ``AgentContext.tools`` 注入，绑定当前 RunSession）。
首版为确定性骨架：首轮 spawn 一个 ``data`` 子 Agent 并 ``wait_for`` 等待；
唤醒后返回结果引用。
"""

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.core.tool_types import ToolContext


class SupervisorAgent(BaseAgent):
    """root SupervisorAgent（确定性骨架）。"""

    def __init__(self) -> None:
        self.spawned: bool = False

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        if not self.spawned:
            self.spawned = True
            tool_ctx = ToolContext("orchestrate", "c1", ctx.emit, ctx.cancel)
            spawn = ctx.tools.resolve("spawn")
            wait = ctx.tools.resolve("wait_for")
            result = await spawn.ainvoke(
                tool_ctx, agent_type="data", content="", name="data-child"
            )
            # wait_for 成功登记后以内部控制流终止本 turn
            await wait.ainvoke(tool_ctx, agent_ids=[result["agent_id"]])
        return AgentOutcome(result_ref="outcome://supervisor")
