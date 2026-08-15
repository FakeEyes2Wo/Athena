"""Unit tests for the LLM rollout trace reader."""

import json

from athena.gui import traces


def _write_trace(tmp_path, agent_id: str, records: list[dict]):
    path = tmp_path / f"{agent_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    return tmp_path


def test_list_traces(tmp_path):
    _write_trace(tmp_path, "agent_a", [{"seq": 0, "ts": "t", "msg": [{"role": "user", "parts": []}]}])
    _write_trace(tmp_path, "agent_b", [{"seq": 0, "ts": "t", "msg": []}])
    summaries = {t["agent_id"]: t for t in traces.list_traces(tmp_path)}
    assert set(summaries) == {"agent_a", "agent_b"}
    assert summaries["agent_a"]["messages"] == 1


def test_read_trace_normalizes_roles(tmp_path):
    records = [
        {
            "seq": 0,
            "ts": "2026-01-01T00:00:00Z",
            "msg": [
                {
                    "role": "user",
                    "parts": [
                        {"kind": "system-prompt", "content": "you are an agent"},
                        {"kind": "user-prompt", "content": "do the thing"},
                    ],
                }
            ],
        },
        {
            "seq": 1,
            "ts": "2026-01-01T00:00:01Z",
            "msg": [
                {
                    "role": "model",
                    "parts": [
                        {"kind": "tool-call", "tool_name": "shell_command", "args": {"command": "ls"}},
                        {"kind": "text", "content": "running…"},
                    ],
                }
            ],
        },
        {"seq": 2, "type": "compaction", "summary": "compacted history"},
    ]
    _write_trace(tmp_path, "agent_a", records)

    result = traces.read_trace(tmp_path, "agent_a")
    assert result["agent_id"] == "agent_a"
    kinds = [(m["role"], m["kind"]) for m in result["messages"]]
    assert kinds == [
        ("system", "system"),
        ("user", "text"),
        ("assistant", "tool_call"),
        ("assistant", "text"),
        ("system", "compaction"),
    ]
    assert result["messages"][2]["args"] == {"command": "ls"}


def test_read_trace_unknown_agent(tmp_path):
    try:
        traces.read_trace(tmp_path, "missing")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown agent trace")
