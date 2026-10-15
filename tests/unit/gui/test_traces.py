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


def test_trace_read_rejects_paths_outside_rollout_directory(tmp_path) -> None:
    from athena.gui.traces import read_trace

    rollout_dir = tmp_path / "agents"
    rollout_dir.mkdir()
    (tmp_path / "outside.jsonl").write_text(
        json.dumps({"seq": 1, "msg": []}), encoding="utf-8"
    )

    try:
        read_trace(rollout_dir, "../outside")
    except KeyError:
        pass
    else:
        raise AssertionError("trace lookup escaped the rollout directory")


def test_trace_tool_arguments_redact_nested_secrets(tmp_path) -> None:
    records = [
        {
            "seq": 1,
            "type": "compaction",
            "summary": "cached password=private-value",
        },
        {
            "seq": 2,
            "msg": [
                {
                    "role": "model",
                    "parts": [
                        {
                            "kind": "tool-call",
                            "tool_name": "shell",
                            "args": '{"api_key":"secret-value",'
                            '"token":"token-value",'
                            '"nested":["access_token=hidden"]}',
                        }
                    ],
                }
            ],
        },
    ]
    (tmp_path / "plan.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records), encoding="utf-8"
    )

    messages = read_trace(tmp_path, "plan")["messages"]

    assert messages[0]["content"] == "cached password=[REDACTED]"
    assert messages[1]["args"] == {
        "api_key": "[REDACTED]",
        "token": "[REDACTED]",
        "nested": ["access_token=[REDACTED]"],
    }


def test_tool_args_only_decode_the_outer_json_string(tmp_path) -> None:
    record = {
        "seq": 1,
        "msg": [
            {
                "role": "model",
                "parts": [
                    {
                        "kind": "tool-call",
                        "tool_name": "query",
                        "args": '{"query":"123","literal":"null","nested":{"secret":"token","text":"true"}}',
                    }
                ],
            }
        ],
    }
    (tmp_path / "plan.jsonl").write_text(json.dumps(record), encoding="utf-8")

    projected = read_trace(tmp_path, "plan")["messages"][0]["args"]

    assert projected == {
        "query": "123",
        "literal": "null",
        "nested": {"secret": "[REDACTED]", "text": "true"},
    }
