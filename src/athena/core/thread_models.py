"""Models describing Athena agent tasks, threads, and turns."""

from pydantic import BaseModel

from athena.core.contracts import ArtifactRef


class AthenaThread(BaseModel):
    """对话线程 — 对应一个 Agent 会话，通过 artifact 引用持久化上下文。"""

    thread_id: str
    session_id: str
    status: str
    context_ref: ArtifactRef


class AthenaTurn(BaseModel):
    """单轮对话 — 包含请求引用和可选的执行结果引用。"""

    turn_id: str
    thread_id: str
    request_ref: ArtifactRef
    status: str
    result_ref: ArtifactRef | None = None
