"""审批 — ``item/approval/request`` 的接收、裁决与回复。

策略只在这一处：闸门永远发问，是否放行由 UI 按当前模式决定。
这与 Claude Code 的 permission mode 一致 —— 服务端负责问，客户端负责判。
"""

import json
from typing import Any

from athena.app_server.protocol import Method, ServerRequest
from athena.core.tool_types import ToolContext
from athena.tui.state import AppState, Block, PendingApproval

ARGS_LIMIT = 96


class ApprovalCoordinator:
    """把 ServerRequest 变成"立即回复"或"挂起等按键"。"""

    def __init__(self, session, state: AppState) -> None:
        self._session = session
        self._state = state

    async def handle(self, request: ServerRequest) -> list[Block]:
        """收到审批请求。auto/deny 立即回复，ask 挂起到 ``state.pending_approval``。"""
        if request.method != Method.ITEM_APPROVAL_REQUEST:
            return [
                Block(
                    "notice",
                    {
                        "text": f"收到未知的服务端请求 {request.method}，已拒绝。",
                        "level": "warn",
                    },
                )
            ]
        params = request.params or {}
        tool = str(params.get("tool") or "tool")
        message = str(params.get("message") or tool)
        if self._state.approval_mode == "auto" or tool in self._state.always_allow:
            await self._session.reply_approval(request.server_call_id, True)
            return []
        if self._state.approval_mode == "deny":
            await self._session.reply_approval(request.server_call_id, False)
            return [
                Block(
                    "notice",
                    {"text": f"deny 模式：已拒绝 {message}", "level": "warn"},
                )
            ]
        self._state.pending_approval = PendingApproval(
            server_call_id=request.server_call_id,
            tool=tool,
            message=message,
            thread_id=str(params.get("thread_id") or ""),
            turn_id=str(params.get("turn_id") or ""),
        )
        return []

    async def resolve(self, decision: str) -> list[Block]:
        """按用户按键裁决。``decision`` ∈ approve / deny / always。"""
        pending = self._state.pending_approval
        if pending is None:
            return []
        self._state.pending_approval = None
        if decision == "always":
            self._state.always_allow.add(pending.tool)
        approved = decision in ("approve", "always")
        await self._session.reply_approval(pending.server_call_id, approved)
        if decision == "always":
            return [
                Block(
                    "notice",
                    {
                        "text": f"本会话将始终批准 {pending.tool}。",
                        "level": "success",
                    },
                )
            ]
        if approved:
            return []
        return [Block("notice", {"text": f"已拒绝 {pending.message}", "level": "warn"})]

    async def flush_denied(self) -> None:
        """退出前把挂起的审批一律拒绝，避免 runner 卡到超时。"""
        pending = self._state.pending_approval
        if pending is None:
            return
        self._state.pending_approval = None
        await self._session.reply_approval(pending.server_call_id, False)


def build_gate(session):
    """构造工具审批闸门 —— 每次工具调用前向 Client 发一次 ServerRequest。"""

    async def gate(ctx: ToolContext, args: dict[str, Any]) -> bool:
        turn_id = ctx.call_id.split(":", 1)[0]
        message = f"{ctx.tool_name}({_summarize(args)})"
        return await session.server.request_approval(
            session.thread_id or "unknown",
            turn_id,
            message,
            payload={"tool": ctx.tool_name, "args": args},
        )

    return gate


def _summarize(args: dict[str, Any]) -> str:
    if not args:
        return ""
    parts = []
    for key, value in args.items():
        if isinstance(value, str):
            rendered = value
        else:
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        parts.append(f"{key}={rendered}")
    joined = ", ".join(parts).replace("\n", "⏎")
    if len(joined) <= ARGS_LIMIT:
        return joined
    return joined[: ARGS_LIMIT - 1] + "…"
