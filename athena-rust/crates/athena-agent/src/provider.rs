use athena_memory::{MessagePart, ModelMessage};
use futures::stream::BoxStream;
use serde_json::{Value, json};
use tokio::sync::watch;

/// Static configuration for one agent.
#[derive(Debug, Clone)]
pub struct AgentConfig {
    pub model: String,
    pub system_prompt: String,
    pub max_turns: usize,
    pub max_tokens: u32,
    pub temperature: f32,
}

impl AgentConfig {
    pub fn new(model: impl Into<String>, system_prompt: impl Into<String>) -> Self {
        Self {
            model: model.into(),
            system_prompt: system_prompt.into(),
            max_turns: 20,
            max_tokens: 4096,
            temperature: 0.1,
        }
    }
}

/// A streamed event from an LLM provider.
#[derive(Debug, Clone, PartialEq)]
pub enum ProviderEvent {
    TextDelta {
        delta: String,
        accumulated: String,
    },
    ToolCall {
        call_id: String,
        name: String,
        arguments: Value,
    },
    ResponseCompleted {
        finish_reason: String,
        accumulated_text: String,
    },
    Error {
        message: String,
    },
}

/// An OpenAI-compatible streaming chat provider.
#[async_trait::async_trait]
pub trait LlmProvider: Send + Sync {
    /// Stream a completion. `tool_specs` are OpenAI tool JSON objects.
    async fn stream(
        &self,
        config: &AgentConfig,
        tool_specs: Vec<Value>,
        messages: Vec<ModelMessage>,
        cancel: watch::Receiver<bool>,
    ) -> BoxStream<'static, ProviderEvent>;
}

/// Map structured messages to OpenAI Chat Completions message dicts, mirroring
/// the Python `_to_api`.
pub fn to_api(messages: &[ModelMessage]) -> Vec<Value> {
    let mut out = Vec::new();
    for msg in messages {
        let tool_calls: Vec<&MessagePart> = msg
            .parts
            .iter()
            .filter(|p| matches!(p, MessagePart::ToolCall { .. }))
            .collect();

        // Assistant message carrying tool calls.
        if !tool_calls.is_empty() {
            let text: String = msg
                .parts
                .iter()
                .filter_map(|p| match p {
                    MessagePart::Text { content } => Some(content.as_str()),
                    _ => None,
                })
                .collect();
            let calls: Vec<Value> = tool_calls
                .iter()
                .filter_map(|p| match p {
                    MessagePart::ToolCall {
                        tool_call_id,
                        tool_name,
                        arguments,
                    } => Some(json!({
                        "id": tool_call_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": arguments},
                    })),
                    _ => None,
                })
                .collect();
            out.push(json!({
                "role": "assistant",
                "content": if text.is_empty() { Value::Null } else { Value::String(text) },
                "tool_calls": calls,
            }));
            continue;
        }

        for part in &msg.parts {
            match part {
                MessagePart::SystemPrompt { content } => {
                    out.push(json!({"role": "system", "content": content}));
                }
                MessagePart::UserPrompt { content } | MessagePart::Text { content } => {
                    out.push(json!({"role": "user", "content": content}));
                }
                MessagePart::ToolReturn {
                    tool_call_id,
                    content,
                    ..
                } => {
                    out.push(json!({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": truncate(content),
                    }));
                }
                MessagePart::ToolCall { .. } => {}
            }
        }
    }
    out
}

fn truncate(content: &str) -> String {
    const MAX: usize = 50_000;
    if content.chars().count() <= MAX {
        return content.to_string();
    }
    let chars: Vec<char> = content.chars().collect();
    let head: String = chars[..24_950].iter().collect();
    let tail: String = chars[chars.len() - 24_950..].iter().collect();
    format!("{head}\n...[TRUNCATED]...\n{tail}")
}
