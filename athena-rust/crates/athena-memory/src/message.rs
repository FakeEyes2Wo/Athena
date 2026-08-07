use serde::{Deserialize, Serialize};

/// Maximum characters allowed in a single `ToolReturn` content before truncation.
pub const MAX_TOOL_RESULT_CHARS: usize = 50_000;

/// Role of a message within a conversation.
///
/// Serialized as `"system"`, `"user"`, `"assistant"`, or `"tool"`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MessageRole {
    System,
    User,
    Assistant,
    Tool,
}

/// A single segment within a [`ModelMessage`].
///
/// Internally tagged by `part_kind` (kebab-case) matching the PydanticAI
/// convention so that serialized output is compatible with the Python
/// message format.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "part_kind", rename_all = "kebab-case")]
pub enum MessagePart {
    SystemPrompt {
        content: String,
    },
    UserPrompt {
        content: String,
    },
    Text {
        content: String,
    },
    ToolCall {
        tool_call_id: String,
        tool_name: String,
        /// JSON-encoded arguments.
        #[serde(rename = "args")]
        arguments: String,
    },
    ToolReturn {
        tool_call_id: String,
        tool_name: String,
        content: String,
    },
}

/// A structured conversation message composed of one or more [`MessagePart`]s.
///
/// Unlike a flat `role + content` model this representation can hold multiple
/// parts (e.g. text + several tool calls) in a single assistant message without
/// data loss.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModelMessage {
    pub role: MessageRole,
    pub parts: Vec<MessagePart>,
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn test_serialize_message_role() {
        assert_eq!(
            serde_json::to_string(&MessageRole::System).unwrap(),
            r#""system""#
        );
        assert_eq!(
            serde_json::to_string(&MessageRole::User).unwrap(),
            r#""user""#
        );
        assert_eq!(
            serde_json::to_string(&MessageRole::Assistant).unwrap(),
            r#""assistant""#
        );
        assert_eq!(
            serde_json::to_string(&MessageRole::Tool).unwrap(),
            r#""tool""#
        );
    }

    #[test]
    fn test_message_role_partial_eq() {
        assert_eq!(MessageRole::User, MessageRole::User);
        assert_ne!(MessageRole::User, MessageRole::Assistant);
    }

    #[test]
    fn test_roundtrip_system_prompt() {
        let part = MessagePart::SystemPrompt {
            content: "You are a bot.".into(),
        };
        let json = serde_json::to_string(&part).unwrap();
        let back: MessagePart = serde_json::from_str(&json).unwrap();
        assert_eq!(part, back);
        assert!(json.contains(r#""part_kind":"system-prompt""#));
        assert!(json.contains(r#""content":"You are a bot.""#));
    }

    #[test]
    fn test_roundtrip_user_prompt() {
        let part = MessagePart::UserPrompt {
            content: "hello".into(),
        };
        let json = serde_json::to_string(&part).unwrap();
        let back: MessagePart = serde_json::from_str(&json).unwrap();
        assert_eq!(part, back);
        assert!(json.contains(r#""part_kind":"user-prompt""#));
    }

    #[test]
    fn test_roundtrip_text() {
        let part = MessagePart::Text {
            content: "Sure, here is the answer.".into(),
        };
        let json = serde_json::to_string(&part).unwrap();
        let back: MessagePart = serde_json::from_str(&json).unwrap();
        assert_eq!(part, back);
        assert!(json.contains(r#""part_kind":"text""#));
    }

    #[test]
    fn test_roundtrip_tool_call() {
        let part = MessagePart::ToolCall {
            tool_call_id: "call_abc".into(),
            tool_name: "search".into(),
            arguments: r#"{"query":"rust"}"#.into(),
        };
        let json = serde_json::to_string(&part).unwrap();
        let back: MessagePart = serde_json::from_str(&json).unwrap();
        assert_eq!(part, back);
        // Field is serialized as "args" not "arguments"
        assert!(json.contains(r#""args":""#));
        assert!(!json.contains(r#""arguments""#));
    }

    #[test]
    fn test_roundtrip_tool_return() {
        let part = MessagePart::ToolReturn {
            tool_call_id: "call_abc".into(),
            tool_name: "search".into(),
            content: "result data".into(),
        };
        let json = serde_json::to_string(&part).unwrap();
        let back: MessagePart = serde_json::from_str(&json).unwrap();
        assert_eq!(part, back);
    }

    #[test]
    fn test_model_message_roundtrip() {
        let msg = ModelMessage {
            role: MessageRole::Assistant,
            parts: vec![
                MessagePart::Text {
                    content: "Let me search.".into(),
                },
                MessagePart::ToolCall {
                    tool_call_id: "call_1".into(),
                    tool_name: "search".into(),
                    arguments: r#"{"q":"test"}"#.into(),
                },
            ],
        };
        let json = serde_json::to_string(&msg).unwrap();
        let back: ModelMessage = serde_json::from_str(&json).unwrap();
        assert_eq!(msg.role, back.role);
        assert_eq!(msg.parts.len(), back.parts.len());
        assert_eq!(msg, back);
    }

    #[test]
    fn test_model_message_serialization_structure() {
        let msg = ModelMessage {
            role: MessageRole::Assistant,
            parts: vec![MessagePart::Text {
                content: "Hello.".into(),
            }],
        };
        let value = serde_json::to_value(&msg).unwrap();
        assert_eq!(value["role"], "assistant");
        assert!(value["parts"].is_array());
        assert_eq!(value["parts"][0]["part_kind"], "text");
        assert_eq!(value["parts"][0]["content"], "Hello.");
    }
}
