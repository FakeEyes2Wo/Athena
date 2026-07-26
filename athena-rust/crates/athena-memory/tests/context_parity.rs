//! Test: parse Python-generated PydanticAI message fixtures into `ModelMessage`
//! and verify that the Rust representation preserves all semantic data losslessly.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use athena_memory::{MessagePart, MessageRole, ModelMessage};
use serde::Deserialize;

/// Mirror the Python PydanticAI "request" / "response" structure so we can
/// deserialize the fixture and then convert to our `ModelMessage`.
#[derive(Debug, Deserialize)]
struct PydanticMessage {
    #[serde(rename = "kind")]
    kind: String,
    parts: Vec<MessagePart>,
}

impl PydanticMessage {
    /// Convert to `ModelMessage`, inferring the role from `kind` and parts.
    fn to_model_message(&self) -> Result<ModelMessage, String> {
        let role = match self.kind.as_str() {
            "request" => match self.parts.first() {
                Some(MessagePart::SystemPrompt { .. }) => MessageRole::System,
                Some(MessagePart::UserPrompt { .. }) => MessageRole::User,
                Some(MessagePart::ToolReturn { .. }) => MessageRole::Tool,
                _ => {
                    return Err("cannot infer role from parts for 'request' kind".to_string());
                }
            },
            "response" => MessageRole::Assistant,
            other => return Err(format!("unknown message kind: {}", other)),
        };
        Ok(ModelMessage {
            role,
            parts: self.parts.clone(),
        })
    }
}

#[derive(Debug, Deserialize)]
struct MessagesFixture {
    assistant_text: Vec<PydanticMessage>,
    assistant_with_tool_calls: Vec<PydanticMessage>,
    system_message: Vec<PydanticMessage>,
    tool_return: Vec<PydanticMessage>,
    user_message: Vec<PydanticMessage>,
}

const MESSAGES_JSON: &str = include_str!("../../../tests/fixtures/messages/messages.json");

fn load_fixture() -> MessagesFixture {
    serde_json::from_str(MESSAGES_JSON).expect("messages.json should be valid JSON")
}

fn assert_message_lossless(pydantic: &PydanticMessage, model: &ModelMessage) {
    // Verify role mapping
    match pydantic.kind.as_str() {
        "request" => match pydantic.parts.first() {
            Some(MessagePart::SystemPrompt { .. }) => assert_eq!(model.role, MessageRole::System),
            Some(MessagePart::UserPrompt { .. }) => assert_eq!(model.role, MessageRole::User),
            Some(MessagePart::ToolReturn { .. }) => assert_eq!(model.role, MessageRole::Tool),
            _ => panic!("unexpected part kind for request"),
        },
        "response" => assert_eq!(model.role, MessageRole::Assistant),
        k => panic!("unknown kind: {}", k),
    }

    // All parts were preserved
    assert_eq!(
        model.parts.len(),
        pydantic.parts.len(),
        "part count mismatch"
    );

    // Each part round-trips through serialization losslessly
    for rust_part in &model.parts {
        let serialized = serde_json::to_value(rust_part).unwrap();
        let deserialized: MessagePart = serde_json::from_value(serialized).unwrap();
        assert_eq!(rust_part, &deserialized, "part round-trip failed");
    }

    // Full message round-trip
    let full_serialized = serde_json::to_value(model).unwrap();
    let full_deserialized: ModelMessage = serde_json::from_value(full_serialized).unwrap();
    assert_eq!(model.role, full_deserialized.role);
    assert_eq!(model.parts.len(), full_deserialized.parts.len());
    for (a, b) in model.parts.iter().zip(full_deserialized.parts.iter()) {
        assert_eq!(a, b, "part mismatch in full message round-trip");
    }
}

#[test]
fn test_assistant_text_lossless() {
    let fixture = load_fixture();
    for msg in &fixture.assistant_text {
        let model = msg.to_model_message().expect("convert assistant_text");
        assert_message_lossless(msg, &model);
    }
}

#[test]
fn test_assistant_with_tool_calls_lossless() {
    let fixture = load_fixture();
    for msg in &fixture.assistant_with_tool_calls {
        let model = msg
            .to_model_message()
            .expect("convert assistant_with_tool_calls");
        assert_message_lossless(msg, &model);
        // Verify multiple parts are preserved
        assert!(
            model.parts.len() >= 2,
            "expected at least 2 parts (text + tool call), got {}",
            model.parts.len()
        );
        // Check that both text and tool-call parts are present
        let has_text = model
            .parts
            .iter()
            .any(|p| matches!(p, MessagePart::Text { .. }));
        let has_tool_call = model
            .parts
            .iter()
            .any(|p| matches!(p, MessagePart::ToolCall { .. }));
        assert!(has_text, "expected a Text part");
        assert!(has_tool_call, "expected a ToolCall part");
    }
}

#[test]
fn test_system_message_lossless() {
    let fixture = load_fixture();
    for msg in &fixture.system_message {
        let model = msg.to_model_message().expect("convert system_message");
        assert_message_lossless(msg, &model);
        assert_eq!(model.role, MessageRole::System);
    }
}

#[test]
fn test_tool_return_lossless() {
    let fixture = load_fixture();
    for msg in &fixture.tool_return {
        let model = msg.to_model_message().expect("convert tool_return");
        assert_message_lossless(msg, &model);
        assert_eq!(model.role, MessageRole::Tool);
        // Verify tool identifiers are preserved
        for part in &model.parts {
            if let MessagePart::ToolReturn {
                tool_call_id,
                tool_name,
                content,
            } = part
            {
                assert!(!tool_call_id.is_empty());
                assert!(!tool_name.is_empty());
                assert!(!content.is_empty());
            }
        }
    }
}

#[test]
fn test_user_message_lossless() {
    let fixture = load_fixture();
    for msg in &fixture.user_message {
        let model = msg.to_model_message().expect("convert user_message");
        assert_message_lossless(msg, &model);
        assert_eq!(model.role, MessageRole::User);
    }
}

#[test]
fn test_all_fixtures_total_count() {
    let fixture = load_fixture();
    let total = fixture.assistant_text.len()
        + fixture.assistant_with_tool_calls.len()
        + fixture.system_message.len()
        + fixture.tool_return.len()
        + fixture.user_message.len();
    assert_eq!(total, 5, "expected 5 fixture messages total");
}

#[test]
fn test_part_kind_values_match_python() {
    // Verify that the serialized part_kind values match Python conventions
    let text = MessagePart::Text {
        content: "hi".into(),
    };
    let json = serde_json::to_value(&text).unwrap();
    assert_eq!(json["part_kind"], "text");

    let sys = MessagePart::SystemPrompt {
        content: "sys".into(),
    };
    let json = serde_json::to_value(&sys).unwrap();
    assert_eq!(json["part_kind"], "system-prompt");

    let user = MessagePart::UserPrompt {
        content: "hi".into(),
    };
    let json = serde_json::to_value(&user).unwrap();
    assert_eq!(json["part_kind"], "user-prompt");

    let tc = MessagePart::ToolCall {
        tool_call_id: "c1".into(),
        tool_name: "t".into(),
        arguments: "{}".into(),
    };
    let json = serde_json::to_value(&tc).unwrap();
    assert_eq!(json["part_kind"], "tool-call");

    let tr = MessagePart::ToolReturn {
        tool_call_id: "c1".into(),
        tool_name: "t".into(),
        content: "ok".into(),
    };
    let json = serde_json::to_value(&tr).unwrap();
    assert_eq!(json["part_kind"], "tool-return");
}
