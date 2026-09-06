"""GUI rollout trace projection contracts."""

import json

from athena.gui.traces import list_traces, read_trace


def test_trace_summary_and_message_projection(tmp_path) -> None:
    path = tmp_path / "plan-1.jsonl"
    records = [
        {"seq": 1, "ts": "t1", "type": "compaction", "summary": "short"},
        {
            "seq": 2,
            "ts": "t2",
            "msg": [
                {"role": "user", "parts": [{"kind": "user-prompt", "content": "go"}]},
                {
                    "role": "model",
                    "parts": [{"kind": "tool-call", "tool_name": "shell", "args": {}}],
                },
            ],
        },
    ]
    path.write_text(
        "not-json\n" + "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )

    summaries = list_traces(tmp_path)
    trace = read_trace(tmp_path, "plan-1")

    assert summaries[0]["agent_id"] == "plan-1"
    assert summaries[0]["messages"] == 1
    assert [(item["kind"], item["content"]) for item in trace["messages"]] == [
        ("compaction", "short"),
        ("text", "go"),
        ("tool_call", "shell"),
    ]
