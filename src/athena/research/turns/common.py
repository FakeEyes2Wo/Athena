"""Shared helpers for agent turn execution.

Kept separate from ``turns.runner`` so the runner and its role modules can all
use the same heartbeat/timeout machinery without circular imports.
"""

import asyncio
import time
from typing import Any

from athena.research.supervisor.events import wait_run_events

AGENT_TURN_TIMEOUT_SECONDS = 900
"""单个 Agent turn 的硬超时。

LLM/工具调用可能因上游无响应而永久挂起（无异常、无事件），前端看起来就是卡住。
给 wait 加超时，至少能把“挂起”变成可读的错误，而不是让 PREPARE 永远停在原地。
"""

TURN_HEARTBEAT_SECONDS = 300
"""等待 Agent turn 期间向 UI 报告“仍在运行”的间隔。"""

MAX_KAGGLE_HANDOFF_CHARS = 12_000
"""注入 Ideator 的 Kaggle handoff 文本上限，防止把超大内容塞进每轮 prompt。"""


async def wait_run_with_heartbeat(
    rt: Any,
    run_id: str,
    *,
    agent_id: str,
    label: str,
    plan: str | None = None,
    project: bool = True,
):
    """Wait for an Agent run, publishing heartbeats and enforcing a hard timeout."""
    agents = rt.agents
    deadline = time.monotonic() + AGENT_TURN_TIMEOUT_SECONDS
    # 心跳超时会取消当前 waiter 并重新转发 journal；游标跨轮保留，否则超过 5 分钟的
    # turn（SEARCH/训练是常态）会把整段 agent 文本和工具调用重新投影一遍。
    forwarded = 0

    def advance(sequence: int) -> None:
        """把游标推进到已成功转发的最新 journal sequence。"""
        nonlocal forwarded
        forwarded = max(forwarded, sequence)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                await agents.interrupt(agent_id, f"{plan or agent_id}_turn_timeout")
            except Exception:
                pass
            raise RuntimeError(f"{label} timed out after {AGENT_TURN_TIMEOUT_SECONDS}s")
        if project:
            events_bus = getattr(rt, "events", None)
            if events_bus is not None:
                publish = lambda kind, ref, data: events_bus.project_agent_event(
                    plan or agent_id, kind, ref, data
                )
            else:
                publish = None
            waiter = wait_run_events(
                agents, run_id, publish, after_sequence=forwarded, on_sequence=advance
            )
        else:
            waiter = agents.wait_run(run_id)
        try:
            return await asyncio.wait_for(
                waiter, timeout=min(TURN_HEARTBEAT_SECONDS, remaining)
            )
        except TimeoutError:
            publish_output = getattr(rt, "publish_output", None)
            if publish_output is not None:
                await publish_output(
                    source="agent",
                    channel="text",
                    text=f"{label} still working (heartbeat)…",
                    plan=plan or agent_id,
                )


def regenerate_prompt(rejections: list[str], target: int) -> str:
    """把逐条拒绝理由拼成给同一个 Ideator 的重新提案请求。

    走 followup 而不是新建 agent：同一个 thread 保留了它原本的探索上下文，知道自己
    提过什么、为什么被拒，否则等于让一个全新的 agent 从零重猜。
    """
    reasons = "\n".join(f"- {reason}" for reason in rejections)
    return (
        "Every hypothesis you proposed was rejected by the quality gate:\n\n"
        f"{reasons}\n\n"
        f"Propose up to {target} different falsifiable hypotheses that address these "
        "specific objections. Do not restate a rejected hypothesis with reworded "
        "prose - change the substance, or explore a different mechanism entirely. "
        "Return the hypotheses as structured output."
    )
