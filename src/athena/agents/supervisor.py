"""root SupervisorAgent（设计 supervisor-agent-design §3、§5）。

动态编排项目内已注册 Agent；只通过 spawn/send/followup/wait_for/wait_for_human
五个受控命令决策（经 ``AgentContext.tools`` 注入，绑定当前 RunSession）。
首版为确定性骨架：首轮 spawn 一个 ``data`` 子 Agent 并 ``wait_for`` 等待；
唤醒后返回结果引用。首轮判定用 ``ctx.input_text``（真实触发有内容，wait 唤醒
为空），不依赖对象临时布尔字段（supervisor-agent-design §9）。
"""

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.core.tool_types import ToolContext


class SupervisorAgent(BaseAgent):
    """root SupervisorAgent（确定性骨架）。"""

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """编排子 Agent：首轮 spawn data 并等待，唤醒后返回结果引用。"""
        if ctx.input_text:
            # 首轮：spawn data-child 并登记等待，wait_for 以内部控制流结束 turn
            tool_ctx = ToolContext("orchestrate", "c1", ctx.emit, ctx.cancel)
            spawn = ctx.tools.resolve("spawn")
            wait = ctx.tools.resolve("wait_for")
            result = await spawn.ainvoke(
                tool_ctx, agent_type="data", content="", name="data-child"
            )
            await wait.ainvoke(tool_ctx, agent_ids=[result.data["agent_id"]])
        return AgentOutcome(result_ref="outcome://supervisor")
