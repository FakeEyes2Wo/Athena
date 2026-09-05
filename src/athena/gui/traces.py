"""Read and normalize deterministic LLM rollout JSONL for the GUI trace viewer.

Rollout files live at ``{project}/.athena/logs/agents/{agent_id}.jsonl``, one
JSON object per line: ``{"seq": int, "ts": iso, "msg": [ModelMessage…]}`` or a
``{"type": "compaction", …}`` checkpoint. ``msg`` is a PydanticAI
``ModelMessagesTypeAdapter`` payload (a list of ModelRequest/ModelResponse).
"""

import json
from pathlib import Path
from typing import Any

from athena.research.supervisor.events import redact


def list_traces(rollout_dir: Path) -> list[dict[str, Any]]:
    """Enumerate agent rollout files as trace summaries."""
    if not rollout_dir.is_dir():
        return []
    traces: list[dict[str, Any]] = []
    for path in sorted(rollout_dir.glob("*.jsonl")):
        try:
            stat = path.stat()
            with path.open("r", encoding="utf-8", errors="replace") as fd:
                messages = sum(1 for line in fd if line.strip() and '"msg"' in line)
        except OSError:
            continue
        traces.append(
            {
                "agent_id": path.stem,
                "file": str(path),
                "size_bytes": stat.st_size,
                "messages": messages,
                "updated_at": stat.st_mtime,
            }
        )
    return traces


def _part_to_message(role: str, part: dict[str, Any]) -> dict[str, Any]:
    """Normalize one PydanticAI message part into a display message."""
    kind = part.get("kind") or part.get("type") or "text"
    if kind in (
        "text",
        "output_text",
        "system-prompt",
        "user-prompt",
        "retry-prompt",
        "tool-return",
    ):
        display_role = role
        display_kind = "text"
        content = part.get("content", "")
        if kind == "system-prompt":
            display_role, display_kind = "system", "system"
        elif kind in ("user-prompt", "retry-prompt"):
            display_role = "user"
        elif kind == "tool-return":
            display_role, display_kind = "tool", "tool_return"
            content = str(content)
        return {
            "role": display_role,
            "kind": display_kind,
            "content": redact(content),
        }
    if kind == "tool-call":
        return {
            "role": "assistant",
            "kind": "tool_call",
            "content": part.get("tool_name", ""),
            "args": part.get("args", {}),
        }
    if kind == "image":
        return {"role": role, "kind": "image", "content": part.get("url", "")}
    return {
        "role": role,
        "kind": "text",
        "content": redact(str(part.get("content", part))),
    }


def _normalize_msg(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize one PydanticAI message dict into a list of display messages."""
    role = msg.get("role")
    parts = msg.get("parts", [])
    if not isinstance(parts, list):
        parts = []
    base_role = "assistant" if role == "model" else "user"
    normalized: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        normalized.append(_part_to_message(base_role, part))
    return normalized


def read_trace(rollout_dir: Path, agent_id: str) -> dict[str, Any]:
    """Read one agent rollout and return its normalized messages in order."""
    path = rollout_dir / f"{agent_id}.jsonl"
    if not path.is_file():
        raise KeyError(f"unknown agent trace: {agent_id}")

    messages: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fd:
        for line in fd:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            seq = record.get("seq", 0)
            ts = record.get("ts", "")
            if record.get("type") == "compaction":
                messages.append(
                    {
                        "seq": seq,
                        "ts": ts,
                        "role": "system",
                        "kind": "compaction",
                        "content": record.get("summary", ""),
                    }
                )
                continue
            payload = record.get("msg")
            if not isinstance(payload, list):
                continue
            for msg in payload:
                if not isinstance(msg, dict):
                    continue
                for display in _normalize_msg(msg):
                    messages.append({"seq": seq, "ts": ts, **display})
    return {"agent_id": agent_id, "messages": messages}
