"""PaperScout 的两个动作在 Athena 侧的工具边界。

工具本身不持有状态，只把调用转发给本次运行的 ``ScoutSession``，因此它们是每次运行
现场构造、注册进一个私有 ``ToolRegistry`` 的，不进全局注册表。

两个工具都声明 ``concurrency_safe=True``：论文的策略明确要求一步内并行发多个调用，
串行化会改变 Agent 的行为特征。池的读改写由 ``ScoutSession`` 内部的锁保证，而不是靠
Agent loop 的串行屏障。
"""

import asyncio

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.research.paper_scout.prompts import (
    EXPAND_ID_DESCRIPTION,
    EXPAND_TOOL_DESCRIPTION,
    SEARCH_QUERY_DESCRIPTION,
    SEARCH_TOOL_DESCRIPTION,
)
from athena.research.paper_scout.session import ScoutSession

SEARCH_TOOL_NAME = "paper_scout_search"
EXPAND_TOOL_NAME = "paper_scout_expand"


class PaperScoutSearchTool(BaseTool):
    """按查询检索新论文并并入 paper pool。"""

    spec = ToolSpec(
        name=SEARCH_TOOL_NAME,
        description=SEARCH_TOOL_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": SEARCH_QUERY_DESCRIPTION,
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    def __init__(self, session: ScoutSession) -> None:
        self.session = session

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """执行一次搜索动作。"""
        query = input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if ctx.cancel.is_set():
            raise asyncio.CancelledError
        action = await self.session.search(query)
        return ToolResult(data=action.model_dump(mode="json"))


class PaperScoutExpandTool(BaseTool):
    """沿池中某篇论文的参考文献扩展一跳。"""

    spec = ToolSpec(
        name=EXPAND_TOOL_NAME,
        description=EXPAND_TOOL_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "arxiv_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": EXPAND_ID_DESCRIPTION,
                }
            },
            "required": ["arxiv_id"],
            "additionalProperties": False,
        },
    )

    def __init__(self, session: ScoutSession) -> None:
        self.session = session

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """执行一次扩展动作。"""
        arxiv_id = input.get("arxiv_id")
        if not isinstance(arxiv_id, str) or not arxiv_id.strip():
            raise ValueError("arxiv_id must be a non-empty string.")
        if ctx.cancel.is_set():
            raise asyncio.CancelledError
        action = await self.session.expand(arxiv_id)
        return ToolResult(data=action.model_dump(mode="json"))
