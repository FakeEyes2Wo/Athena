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
        agent_id = path.stem
        messages = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fd:
                for line in fd:
                    if line.strip() and '"msg"' in line:
                        messages += 1
        except OSError:
            continue
        traces.append(
            {
                "agent_id": agent_id,
                "file": str(path),
                "size_bytes": path.stat().st_size,
                "messages": messages,
                "updated_at": path.stat().st_mtime,
            }
        )
    return traces


def _part_to_message(role: str, part: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one PydanticAI message part into a display message."""
    kind = part.get("kind") or part.get("type") or "text"
    if kind in ("text", "output_text"):
        return {"role": role, "kind": "text", "content": redact(part.get("content", ""))}
    if kind == "system-prompt":
        return {"role": "system", "kind": "system", "content": redact(part.get("content", ""))}
    if kind in ("user-prompt", "retry-prompt"):
        return {"role": "user", "kind": "text", "content": redact(part.get("content", ""))}
    if kind == "tool-return":
        return {"role": "tool", "kind": "tool_return", "content": redact(str(part.get("content", "")))}
    if kind == "tool-call":
        return {
            "role": "assistant",
            "kind": "tool_call",
            "content": part.get("tool_name", ""),
            "args": part.get("args", {}),
        }
    if kind == "image":
        return {"role": role, "kind": "image", "content": part.get("url", "")}
    return {"role": role, "kind": "text", "content": redact(str(part.get("content", part)))}


def normalize_msg(msg: dict[str, Any]) -> list[dict[str, Any]]:
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
        display = _part_to_message(base_role, part)
        if display is not None:
            normalized.append(display)
    return normalized


def read_trace(rollout_dir: Path, agent_id: str) -> dict[str, Any]:
    """Read one agent rollout and return its normalized messages in order."""
    candidates = [rollout_dir / f"{agent_id}.jsonl"]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
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
                for display in normalize_msg(msg):
                    messages.append({"seq": seq, "ts": ts, **display})
    return {"agent_id": agent_id, "messages": messages}
