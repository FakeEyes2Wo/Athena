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


def test_trace_projection_reads_pydantic_ai_dumps(tmp_path) -> None:
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter,
        ModelRequest,
        ModelResponse,
        SystemPromptPart,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    history = [
        ModelRequest(
            parts=[SystemPromptPart(content="sys"), UserPromptPart(content="go")]
        ),
        ModelResponse(
            parts=[
                TextPart(content="thinking"),
                ToolCallPart(
                    tool_name="shell", args='{"command": "ls"}', tool_call_id="c1"
                ),
            ]
        ),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="shell", content="ok", tool_call_id="c1")]
        ),
    ]
    payload = ModelMessagesTypeAdapter.dump_python(history, mode="json")
    (tmp_path / "plan-2.jsonl").write_text(
        json.dumps({"seq": 0, "ts": "t", "msg": payload}), encoding="utf-8"
    )

    trace = read_trace(tmp_path, "plan-2")

    assert [(m["role"], m["kind"], m["content"]) for m in trace["messages"]] == [
        ("system", "system", "sys"),
        ("user", "text", "go"),
        ("assistant", "text", "thinking"),
        ("assistant", "tool_call", "shell"),
        ("tool", "tool_return", "ok"),
    ]
    assert trace["messages"][3]["args"] == {"command": "ls"}
