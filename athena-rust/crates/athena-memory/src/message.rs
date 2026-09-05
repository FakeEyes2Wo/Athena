use serde::{Deserialize, Serialize};

const MAX_TOOL_RESULT_CHARS: usize = 50_000;

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

/// An isolated conversation buffer that normalizes messages on insertion.
#[derive(Default)]
pub struct ContextManager {
    items: Vec<ModelMessage>,
}

impl ContextManager {
    pub fn new() -> Self {
        Self::default()
    }

    /// Clone the conversation so providers cannot mutate stored history.
    pub fn items(&self) -> Vec<ModelMessage> {
        self.items.clone()
    }

    pub fn append(&mut self, mut message: ModelMessage) {
        for part in &mut message.parts {
            let MessagePart::ToolReturn { content, .. } = part else {
                continue;
            };
            if content.len() <= MAX_TOOL_RESULT_CHARS {
                continue;
            }

            let budget = MAX_TOOL_RESULT_CHARS - 50;
            let head = budget / 2;
            let tail = budget - head;
            *content = format!(
                "{}...\n...[TRUNCATED]...\n{}",
                &content[..head],
                &content[content.len() - tail..]
            );
        }
        self.items.push(message);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn message(role: MessageRole, part: MessagePart) -> ModelMessage {
        ModelMessage {
            role,
            parts: vec![part],
        }
    }

    #[test]
    fn context_starts_empty_and_returns_detached_history() {
        let mut context = ContextManager::new();
        context.append(message(
            MessageRole::User,
            MessagePart::UserPrompt {
                content: "original".into(),
            },
        ));

        let mut history = context.items();
        history[0].parts.clear();
        assert_eq!(context.items()[0].parts.len(), 1);
    }

    #[test]
    fn context_truncates_only_long_tool_results() {
        let mut context = ContextManager::new();
        context.append(message(
            MessageRole::Tool,
            MessagePart::ToolReturn {
                tool_call_id: "call".into(),
                tool_name: "tool".into(),
                content: "A".repeat(60_000),
            },
        ));
        context.append(message(
            MessageRole::User,
            MessagePart::UserPrompt {
                content: "B".repeat(60_000),
            },
        ));

        let history = context.items();
        assert!(matches!(
            &history[0].parts[0],
            MessagePart::ToolReturn { content, .. }
                if content.len() < 60_000 && content.contains("[TRUNCATED]")
        ));
        assert!(matches!(
            &history[1].parts[0],
            MessagePart::UserPrompt { content } if content.len() == 60_000
        ));
    }
}
