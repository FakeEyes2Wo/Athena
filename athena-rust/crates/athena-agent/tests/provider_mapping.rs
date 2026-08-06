//! `to_api` message-mapping parity with the Python `_to_api`.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use athena_agent::to_api;
use athena_memory::{MessagePart, MessageRole, ModelMessage};

#[test]
fn maps_roles_and_tool_calls() {
    let messages = vec![
        ModelMessage {
            role: MessageRole::System,
            parts: vec![MessagePart::SystemPrompt {
                content: "sys".into(),
            }],
        },
        ModelMessage {
            role: MessageRole::User,
            parts: vec![MessagePart::UserPrompt {
                content: "hi".into(),
            }],
        },
        ModelMessage {
            role: MessageRole::Assistant,
            parts: vec![
                MessagePart::Text {
                    content: "let me search".into(),
                },
                MessagePart::ToolCall {
                    tool_call_id: "call_1".into(),
                    tool_name: "search".into(),
                    arguments: r#"{"q":"x"}"#.into(),
                },
            ],
        },
        ModelMessage {
            role: MessageRole::Tool,
            parts: vec![MessagePart::ToolReturn {
                tool_call_id: "call_1".into(),
                tool_name: "search".into(),
                content: "result".into(),
            }],
        },
    ];

    let api = to_api(&messages);
    assert_eq!(api[0]["role"], "system");
    assert_eq!(api[1]["role"], "user");
    assert_eq!(api[2]["role"], "assistant");
    assert_eq!(api[2]["tool_calls"][0]["id"], "call_1");
    assert_eq!(api[2]["tool_calls"][0]["function"]["name"], "search");
    assert_eq!(api[3]["role"], "tool");
    assert_eq!(api[3]["tool_call_id"], "call_1");
    assert_eq!(api[3]["content"], "result");
}
