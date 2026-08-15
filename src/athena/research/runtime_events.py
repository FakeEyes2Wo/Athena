"""Runtime output/state projection, subscription, and resume persistence.

拆自 ``ResearchRuntime``：把"事件投影 + 订阅 + TUI 断点续传持久化"独立成
一个组合单元。Supervisor 通过 ``attach_supervisor`` 延迟注入，打破构造期
的循环依赖（Supervisor 的回调引用本类方法，本类的投影又需要 Supervisor
持有的 state/tree）。
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from athena.core.contracts import ArtifactRef, ArtifactStore, new_id
from athena.execution.runtime import CommandResult
from athena.research.supervisor.events import (
    EventProjector,
    StateEvent,
    redact,
    sanitize_terminal_text,
    truncate_middle,
)
from athena.research.supervisor.scheduler import count_search_attempts
from athena.research.supervisor.supervisor import Supervisor

logger = logging.getLogger(__name__)

# shell_command 展示文本的字符上限：超长命令居中截断，保留首尾（对齐 codex）。
_MAX_COMMAND_CHARS = 400

EmitFn = Callable[[str, dict[str, object]], Awaitable[None] | None]


class RuntimeEvents:
    """Project, publish, and persist the runtime's output/state stream."""

    def __init__(
        self,
        *,
        events: EventProjector,
        store: ArtifactStore,
        sessions_dir: Path,
    ) -> None:
        self._events = events
        self._store = store
        self._sessions_dir = sessions_dir
        self._supervisor: Supervisor | None = None
        self._ideator_lanes = 0
        self._subscribers: dict[str, EmitFn] = {}
        self._subscriber_ready: dict[str, asyncio.Task[object]] = {}

    @property
    def _log_path(self) -> Path:
        """This runtime's transcript file (one transcript per runtime)."""
        return self._sessions_dir / "default.jsonl"

    def attach_supervisor(self, supervisor: Supervisor) -> None:
        """Inject the Supervisor after construction (breaks the build cycle)."""
        self._supervisor = supervisor

    def set_ideator_lanes(self, count: int) -> None:
        """Record the concurrent Ideator lane count for state projection."""
        self._ideator_lanes = count

    def subscribe(self, emit: EmitFn) -> str:
        """Subscribe and immediately receive one complete state snapshot."""
        subscription_id = new_id("research")
        self._subscribers[subscription_id] = emit
        result = emit("state", self._state_event().model_dump(mode="json"))
        if asyncio.iscoroutine(result):
            self._subscriber_ready[subscription_id] = asyncio.create_task(result)
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> None:
        self._subscribers.pop(subscription_id, None)
        ready = self._subscriber_ready.pop(subscription_id, None)
        if ready is not None and not ready.done():
            ready.cancel()

    async def publish_output(
        self,
        *,
        source: Literal["supervisor", "agent", "tool"],
        channel: Literal["text", "stdout", "stderr", "error"],
        text: str,
        plan: str | None = None,
        tool: str | None = None,
        artifact_ref: ArtifactRef | None = None,
    ) -> None:
        event = self._events.output(
            source=source,
            channel=channel,
            text=text,
            plan=plan,
            tool=tool,
            artifact_ref=artifact_ref,
        )
        await self._publish("output", event.model_dump(mode="json"))

    async def project_command_result(
        self,
        result: CommandResult,
        *,
        plan: str | None = None,
        tool: str = "shell_command",
    ) -> None:
        """Publish one completed command as exactly one safe output event."""
        artifact_ref = result.output_ref
        if artifact_ref is not None:
            full = await self._store.get_text(artifact_ref)
            safe_full = redact(sanitize_terminal_text(full))
            if safe_full != full:
                unsafe_ref = artifact_ref
                artifact_ref = await self._store.put_text(safe_full)
                await asyncio.to_thread(
                    self._store.path_for(unsafe_ref).unlink, missing_ok=True
                )
        event = await self._events.tool_output(
            stdout=result.stdout,
            stderr=result.stderr,
            plan=plan,
            tool=tool,
            artifact_ref=artifact_ref,
        )
        await self._publish("output", event.model_dump(mode="json"))

    @staticmethod
    def _is_ideator_plan(plan: str) -> bool:
        """True when ``plan`` labels a concurrent Ideator lane (``ideator-N``)."""
        return plan.startswith("ideator-")

    async def publish_ideator_state(self) -> None:
        """Announce the current Ideator lane count before lanes start streaming."""
        if not self._subscribers:
            return
        await self._publish("state", self._state_event().model_dump(mode="json"))

    async def project_agent_event(
        self,
        plan: str,
        kind: str,
        _event_ref: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Project one Agent journal event into a display output record."""
        assert self._supervisor is not None
        payload = data or {}
        ideator = self._is_ideator_plan(plan)
        if kind == "agent/text_delta":
            text = str(payload.get("delta") or payload.get("accumulated") or "")
            if ideator:
                # Ideator 的流式 delta 常以换行结尾，逐 token 刷屏；去掉末尾换行。
                text = text.rstrip("\n\r")
            # 流式 deltas 常为纯空白（换行/缩进），显示无信息量且会刷出空行。
            if text.strip():
                await self.publish_output(
                    source="agent", channel="text", text=text, plan=plan
                )
        elif kind == "agent/function_call":
            # Ideator 只展示 LLM 话语，工具调用不进入显示流，便于阅读。
            if ideator:
                return
            name = str(payload.get("name") or "tool")
            args = payload.get("arguments")
            if args:
                # 超长 shell 命令居中截断，保留首尾。
                if (
                    name == "shell_command"
                    and isinstance(args, dict)
                    and isinstance(args.get("command"), str)
                ):
                    command = truncate_middle(args["command"], _MAX_COMMAND_CHARS)
                    args = {**args, "command": command}
                try:
                    args_text = json.dumps(args, ensure_ascii=False)
                except (TypeError, ValueError):
                    args_text = str(args)
                text = f"{name}({args_text})"
            else:
                text = f"{name}()"
            await self.publish_output(
                source="agent",
                channel="text",
                text=text,
                plan=plan,
                tool=name,
            )
        elif kind == "command/completed":
            if ideator:
                return
            await self.project_command_result(CommandResult(**payload), plan=plan)
        elif kind == "tool/end":
            tool = str(payload.get("tool") or "")
            path = payload.get("path")
            if tool not in {"read_file", "write_file"} or not isinstance(path, str):
                return
            plan_state = self._supervisor.state.plans.get(plan)
            if plan_state is None or plan_state.kind != "SEARCH":
                return
            try:
                worktree = self._supervisor.workspace_path(plan)
            except KeyError:
                return
            text = f"{path}\nworktree: {worktree}"
            if tool == "write_file":
                root = worktree.resolve()
                written = (root / path).resolve()
                if written.is_relative_to(root) and written.is_file():
                    content = await asyncio.to_thread(
                        written.read_text, encoding="utf-8"
                    )
                    text = f"{text}\n\n{content}"
            event = await self._events.tool_output(
                stdout=text,
                plan=plan,
                tool=tool,
            )
            await self._publish("output", event.model_dump(mode="json"))

    async def publish_from_supervisor(
        self, kind: Literal["output", "state"], payload: dict[str, object]
    ) -> None:
        if kind == "state":
            payload = self._state_event().model_dump(mode="json")
        elif kind == "output":
            # Supervisor 的 output 是裸 dict（无 seq/type），统一经 EventProjector
            # 投影成合法 OutputEvent。
            event = self._events.output(
                source=payload.get("source", "supervisor"),
                channel=payload.get("channel", "text"),
                text=str(payload.get("text", "")),
                plan=payload.get("plan"),
                tool=payload.get("tool"),
            )
            payload = event.model_dump(mode="json")
        await self._publish(kind, payload)

    async def _publish(self, kind: str, payload: dict[str, object]) -> None:
        if kind not in {"output", "state"}:
            raise ValueError("runtime events must be output or state")
        if kind == "output":
            self._append_log(payload)

        async def invoke(subscription_id: str, emit: EmitFn) -> None:
            try:
                ready = self._subscriber_ready.pop(subscription_id, None)
                if ready is not None:
                    await ready
                result = emit(kind, payload)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "subscriber %s failed handling %s", subscription_id, kind
                )

        await asyncio.gather(
            *(invoke(key, emit) for key, emit in list(self._subscribers.items()))
        )

    def _append_log(self, record: dict[str, object]) -> None:
        """Append one session record to the active session's transcript (best-effort)."""
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("failed to persist session record")

    def persist_user_message(self, text: str) -> None:
        """Append one Human message to the active session, sharing the output seq."""
        self._append_log(
            {"type": "user", "seq": self._events._next_sequence(), "text": text}
        )

    def resume_sequence(self) -> None:
        """Resume the seq counter past this runtime's persisted transcript."""
        max_seq = -1
        for record in self.replay_output_events():
            seq = record.get("seq")
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq
        if max_seq >= 0:
            self._events.resume(max_seq)

    def replay_output_events(self) -> list[dict[str, object]]:
        """Return this runtime's persisted transcript records."""
        path = self._log_path
        if not path.is_file():
            return []
        events: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skipping malformed output log line")
        return events

    def _state_event(self) -> StateEvent:
        assert self._supervisor is not None
        state = self._supervisor.state
        tree = self._supervisor.tree
        plans = [
            {"id": plan_id, **plan.model_dump(mode="json")}
            for plan_id, plan in state.plans.items()
        ]
        successes = sum(
            experiment.status.value == "SUCCEEDED"
            for experiment in tree.experiments(kind="search")
        )
        sota_id = tree.best_experiment_id()
        sota = None
        if sota_id is not None:
            experiment = tree.get_experiment(sota_id)
            sota = {
                "experiment": sota_id,
                "metric": experiment.eval.primary if experiment.eval else None,
                "commit": experiment.commit,
            }
        waiting_ids = [
            plan_id
            for plan_id, plan in state.plans.items()
            if plan.turn_limit is not None and plan.turns_used >= plan.turn_limit
        ]
        pending = [
            {"id": h.id, "statement": h.statement}
            for h in tree.pending_hypotheses()
            if h.id is not None
        ]
        return StateEvent(
            status=state.status,
            phase=state.phase,
            plans=plans,
            search={
                "attempts": count_search_attempts(state, tree),
                "limit": state.search_limit,
                "successes": successes,
                "concurrency": state.concurrency,
                "ideator_lanes": self._ideator_lanes,
            },
            sota=sota,
            waiting=(
                {"plans": waiting_ids, "reason": "turn_limit_exhausted"}
                if waiting_ids
                else None
            ),
            manual=state.manual_mode,
            pending=pending,
            validation=state.validation,
            eda_dir=state.eda_dir,
            task_understanding=state.task_understanding,
        )
