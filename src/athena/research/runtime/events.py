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
from athena.research.runtime.event_projection import supervisor_output, supervisor_state
from athena.research.supervisor.events import (
    EventProjector,
    redact,
    sanitize_terminal_text,
    truncate_middle,
)
from athena.research.supervisor.supervisor import Supervisor

logger = logging.getLogger(__name__)

# shell_command 展示文本的字符上限：超长命令居中截断，保留首尾（对齐 codex）。
_MAX_COMMAND_CHARS = 400

EmitFn = Callable[[str, dict[str, object]], Awaitable[None] | None]

# Agent 流式增量不再逐 token 持久化到 session transcript。实时订阅仍逐 delta
# 推送；落盘时在消息边界（函数调用 / turn 终态）合并成一条完整文本。
_AGENT_TEXT_BOUNDARY_KINDS = {
    "agent/function_call",
    "turn_completed",
    "turn_failed",
    "turn_interrupted",
}


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
        self._agent_buffers: dict[str, dict[str, Any]] = {}

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
        result = emit(
            "state",
            supervisor_state(self._supervisor, self._ideator_lanes).model_dump(
                mode="json"
            ),
        )
        if asyncio.iscoroutine(result):
            self._subscriber_ready[subscription_id] = asyncio.create_task(result)
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove one runtime event subscriber and cancel its pending snapshot."""
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
        persist: bool = True,
        message_id: str | None = None,
        session_id: str | None = None,
        scope: str | None = None,
        scope_id: str | None = None,
    ) -> None:
        """Project and publish one human-readable runtime output record."""
        event = self._events.output(
            source=source,
            channel=channel,
            text=text,
            plan=plan,
            tool=tool,
            artifact_ref=artifact_ref,
            message_id=message_id,
            session_id=session_id,
            scope=scope,
            scope_id=scope_id,
        )
        await self._publish("output", event.model_dump(mode="json"), log=persist)

    async def publish_clarification(self, session_id: str, draft: object) -> None:
        """Publish one authoritative clarification draft snapshot."""
        payload = (
            draft.model_dump(mode="json") if hasattr(draft, "model_dump") else draft
        )
        await self._publish(
            "clarification",
            {"session_id": session_id, "draft": payload},
            log=False,
        )

    async def publish_human_request(
        self,
        session_id: str,
        action: str,
        request: object | None,
        outcome: object | None = None,
    ) -> None:
        """Publish a created/settled human request event."""
        request_payload = (
            request.model_dump(mode="json")
            if hasattr(request, "model_dump")
            else request
        )
        outcome_payload = (
            outcome.model_dump(mode="json")
            if hasattr(outcome, "model_dump")
            else outcome
        )
        await self._publish(
            "human_request",
            {
                "session_id": session_id,
                "action": action,
                "request": request_payload,
                "outcome": outcome_payload,
            },
            log=False,
        )

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
        if not result.stdout and not result.stderr:
            event = self._events.output(
                source="tool",
                channel="stdout",
                text=f"{tool} completed (exit {result.exit_code}, no output)",
                plan=plan,
                tool=tool,
            )
        else:
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

    def _flush_agent_text(self, plan: str) -> None:
        """Persist one complete Agent text message at a natural boundary."""
        buffer = self._agent_buffers.pop(plan or "", None)
        if not buffer or not buffer.get("text"):
            return
        event = self._events.output(
            source="agent",
            channel="text",
            text=buffer["text"],
            plan=buffer.get("plan"),
            message_id=buffer.get("message_id"),
        )
        self._append_log(event.model_dump(mode="json"))

    async def publish_ideator_state(self) -> None:
        """Announce the current Ideator lane count before lanes start streaming."""
        if not self._subscribers:
            return
        await self._publish(
            "state",
            supervisor_state(self._supervisor, self._ideator_lanes).model_dump(
                mode="json"
            ),
        )

    async def _project_agent_text(
        self,
        plan: str,
        payload: dict[str, Any],
        *,
        ideator: bool,
    ) -> None:
        """Stream one Agent text delta and retain its complete message buffer."""
        raw = str(payload.get("delta") or payload.get("accumulated") or "")
        if not raw:
            return
        text = raw.rstrip("\n\r") if ideator else raw
        key = plan or ""
        previous = self._agent_buffers.get(key)
        message_id = previous["message_id"] if previous else new_id("msg")

        # Suppress leading whitespace-only display items without dropping spaces
        # that arrive as standalone deltas after the message body has started.
        has_content = bool(previous and str(previous["text"]).strip())
        if text.strip() or has_content:
            await self.publish_output(
                source="agent",
                channel="text",
                text=text,
                plan=plan,
                persist=False,
                message_id=message_id,
            )
        full_text = str(payload.get("accumulated") or "")
        if not full_text:
            full_text = (previous["text"] + raw) if previous else raw
        self._agent_buffers[key] = {
            "text": full_text,
            "plan": plan,
            "message_id": message_id,
        }

    async def _project_agent_tool(
        self,
        plan: str,
        kind: str,
        payload: dict[str, Any],
        *,
        ideator: bool,
    ) -> None:
        """Project Agent tool calls, command results, and file effects."""
        if ideator:
            return
        if kind == "agent/function_call":
            name = str(payload.get("name") or "tool")
            args = payload.get("arguments")
            if args:
                if (
                    name == "shell_command"
                    and isinstance(args, dict)
                    and isinstance(args.get("command"), str)
                ):
                    args = {
                        **args,
                        "command": truncate_middle(
                            args["command"],
                            _MAX_COMMAND_CHARS,
                        ),
                    }
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
            return
        if kind == "command/completed":
            await self.project_command_result(CommandResult(**payload), plan=plan)
            return
        if kind != "tool/end":
            return

        tool = str(payload.get("tool") or "")
        path = payload.get("path")
        if tool not in {"read_file", "write_file"} or not isinstance(path, str):
            return
        assert self._supervisor is not None
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
                    written.read_text,
                    encoding="utf-8",
                )
                text = f"{text}\n\n{content}"
        event = await self._events.tool_output(stdout=text, plan=plan, tool=tool)
        await self._publish("output", event.model_dump(mode="json"))

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
        if kind in _AGENT_TEXT_BOUNDARY_KINDS:
            self._flush_agent_text(plan)
        if kind == "agent/text_delta":
            await self._project_agent_text(plan, payload, ideator=ideator)
            return
        await self._project_agent_tool(plan, kind, payload, ideator=ideator)

    async def publish_from_supervisor(
        self, kind: Literal["output", "state"], payload: dict[str, object]
    ) -> None:
        """Normalize and publish one Supervisor output or state projection."""
        if kind == "state":
            payload = supervisor_state(
                self._supervisor, self._ideator_lanes
            ).model_dump(mode="json")
        elif kind == "output":
            payload = supervisor_output(self._events, payload)
        await self._publish(kind, payload)

    async def _publish(
        self,
        kind: str,
        payload: dict[str, object],
        *,
        log: bool = True,
    ) -> None:
        if kind not in {"output", "state", "clarification", "human_request"}:
            raise ValueError(
                "runtime events must be output, state, clarification, or human_request"
            )
        if kind == "output" and log:
            self._append_log(payload)

        async def invoke(subscription_id: str, emit: EmitFn) -> None:
            """Deliver one event to a subscriber after its initial snapshot."""
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

    async def aclose(self) -> None:
        """Cancel pending subscriber handshakes and drop all subscribers."""
        # Flush any in-flight Agent text so an interrupted runtime still leaves a
        # complete message in the resume transcript, not a dangling delta buffer.
        for plan in list(self._agent_buffers):
            self._flush_agent_text(plan)
        for ready in self._subscriber_ready.values():
            if not ready.done():
                ready.cancel()
        self._subscriber_ready.clear()
        self._subscribers.clear()

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
            {"type": "user", "seq": self._events.next_sequence(), "text": text}
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
