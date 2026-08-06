"""AgentControl — 面向调用方的能力门面（设计 §1.2、§2.4）。"""

from collections.abc import AsyncIterator, Collection
from typing import Any, Generic, TYPE_CHECKING

from athena.core.agent_kernel.types import (
    AgentCommandError,
    AgentEvent,
    AgentId,
    AgentPath,
    AgentRunFailed,
    AgentRunInterrupted,
    AgentSnapshot,
    AgentSpec,
    AgentStatus,
    AgentWaitResult,
    ErrorCode,
    ForkPolicy,
    ResponseT,
    ReturnWhen,
    RunId,
    RunStatus,
    RunSummary,
)

if TYPE_CHECKING:
    from athena.core.agent_kernel.kernel import AgentKernel


class AgentHandle:
    """指向稳定 Agent 身份的 capability；状态实时查询 Kernel（§2.3）。

    绑定创建它的 ``AgentControl``；跨内核使用会被拒绝（R1-9）。
    """

    def __init__(self, agent_id: AgentId, control: "AgentControl") -> None:
        self.agent_id = agent_id
        self._control = control

    def _check_control(self, control: "AgentControl") -> None:
        if control is not self._control:
            raise AgentCommandError(
                ErrorCode.NOT_FOUND, "agent handle belongs to another kernel"
            )

    @property
    def path(self) -> AgentPath:
        """Agent 的稳定树内路径（实时查询 Kernel）。"""
        return self._snapshot().path

    @property
    def name(self) -> str:
        """Agent 名称。"""
        return self._snapshot().name

    @property
    def role(self) -> str:
        """Agent 角色。"""
        return self._snapshot().role

    @property
    def status(self) -> AgentStatus:
        """Agent 当前生命周期状态。"""
        status = self._control.kernel.agent_status(self.agent_id)
        if status is None:
            raise KeyError(f"unknown agent: {self.agent_id}")
        return status

    async def snapshot(self) -> AgentSnapshot:
        """返回 Agent 元数据快照。"""
        return self._snapshot()

    def events(self, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        """读取本 Agent 的 Session journal（跨 Run，§2.3）。"""
        return self._control.kernel.session_events(self.agent_id, after_sequence)

    def _snapshot(self) -> AgentSnapshot:
        snap = self._control.kernel.agent_snapshot(self.agent_id)
        if snap is None:
            raise KeyError(f"unknown agent: {self.agent_id}")
        return snap


class AgentRun(Generic[ResponseT]):
    """指向一次 Run 的 capability；不拥有执行 task（§2.3）。"""

    def __init__(self, run_id: RunId, control: "AgentControl", codec: Any) -> None:
        self.run_id = run_id
        self._control = control
        self._codec = codec

    async def wait(self, timeout: float | None = None) -> ResponseT:
        """等待 Run 终态并解码响应；INTERRUPTED/FAILED 抛领域异常。"""
        summary = await self._control.kernel.wait_run(self.run_id, timeout=timeout)
        if summary.status == RunStatus.INTERRUPTED:
            raise AgentRunInterrupted(summary.reason or "run interrupted")
        if summary.status == RunStatus.FAILED:
            raise AgentRunFailed(summary.error or "run failed")
        if summary.response_ref is None:
            raise RuntimeError("completed run has no response reference")
        try:
            return self._codec.decode_response(summary.response_ref)
        except Exception as exc:
            # 响应解码失败 → 只暴露异常类型且断链，防凭据/响应内容泄露（B10）
            raise AgentRunFailed(
                f"response decode failed: {type(exc).__name__}"
            ) from None

    async def cancel(self, reason: str = "caller_cancelled") -> None:
        """取消本 Run 并终止执行。"""
        await self._control.kernel.cancel_run(self.run_id, reason=reason)

    def events(self, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        """读取本 Run 的事件流（订阅式，§2.3）。"""
        return self._control.kernel.run_events(self.run_id, after_sequence)

    async def summary(self) -> RunSummary:
        """返回本 Run 的当前摘要（可能仍在运行）；需要终态请用 ``wait()``。"""
        summary = self._control.kernel.run_summary(self.run_id)
        if summary is None:
            raise KeyError(f"unknown run: {self.run_id}")
        return summary


class AgentControl:
    """对 Agent 树执行命令的唯一公开控制面（§2.4）。"""

    def __init__(self, kernel: "AgentKernel") -> None:
        self.kernel = kernel

    async def create_root(
        self, spec: AgentSpec, task: object, *, name: str = "root"
    ) -> tuple[AgentHandle, AgentRun]:
        """创建根 Agent 并返回其 handle 与首个 Run。"""
        agent_id, run_id = await self.kernel.create_root(spec, task, name=name)
        return AgentHandle(agent_id, self), AgentRun(run_id, self, spec.codec)

    async def spawn(
        self,
        parent: AgentHandle,
        spec: AgentSpec,
        task: object,
        *,
        name: str | None = None,
        fork: ForkPolicy = ForkPolicy.none(),
    ) -> tuple[AgentHandle, AgentRun]:
        """在 parent 下创建子 Agent 并返回其 handle 与 Run。"""
        parent._check_control(self)
        agent_id, run_id = await self.kernel.spawn(
            parent.agent_id, spec, task, name=name, fork=fork
        )
        return AgentHandle(agent_id, self), AgentRun(run_id, self, spec.codec)

    async def send_message(self, target: AgentHandle, message: object) -> None:
        """向目标 mailbox 投递业务 payload；envelope 由 Kernel 权威生成（§3.3，B9）。"""
        target._check_control(self)
        await self.kernel.send_message(target.agent_id, message)

    async def followup(self, target: AgentHandle, task: object) -> AgentRun:
        """向目标 Agent 投递后续任务并返回新 Run。"""
        target._check_control(self)
        run_id = await self.kernel.followup(target.agent_id, task)
        spec = self.kernel.registry_spec(target.agent_id)
        return AgentRun(run_id, self, spec.codec)

    async def wait_agent(
        self,
        targets: Collection[AgentHandle],
        *,
        return_when: ReturnWhen = ReturnWhen.FIRST_COMPLETED,
        timeout: float | None = None,
    ) -> AgentWaitResult:
        """等待一组 Agent 当前 Run 终结；timeout 返回部分状态，不取消目标（§3.6）。"""
        for target in targets:
            target._check_control(self)
        return await self.kernel.wait_agent(
            [t.agent_id for t in targets], return_when=return_when, timeout=timeout
        )

    async def interrupt(self, target: AgentHandle, reason: str) -> None:
        """中断目标 Agent 的当前 Run（§3.5）。"""
        target._check_control(self)
        await self.kernel.interrupt(target.agent_id, reason)

    def list_agents(self, path_prefix: AgentPath | None = None) -> list[AgentSnapshot]:
        """列出 Agent 快照，可按路径前缀过滤。"""
        return self.kernel.list_agents(path_prefix)

    async def close(self, target: AgentHandle, *, recursive: bool = False) -> None:
        """关闭目标 Agent 子树；recursive=False 遇活子孙失败（§3.5）。"""
        target._check_control(self)
        await self.kernel.close(target.agent_id, recursive=recursive)

    async def aclose(self) -> None:
        """关闭整个 Kernel 与全部 Agent 资源。"""
        await self.kernel.aclose()
