use crate::message::{MAX_TOOL_RESULT_CHARS, MessagePart, ModelMessage};

/// Opaque token-invariant snapshot used for rollback.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ContextSnapshot {
    pub index: usize,
    pub version: u64,
}

/// Manages a sequence of [`ModelMessage`]s while maintaining the invariant:
///
/// `self.token_count == sum(Self::estimate_tokens(m) for m in self.items)`
pub struct ContextManager {
    items: Vec<ModelMessage>,
    token_count: usize,
    context_limit: usize,
    version: u64,
}

impl ContextManager {
    pub fn new(context_limit: usize) -> Self {
        Self {
            items: Vec::new(),
            token_count: 0,
            context_limit,
            version: 0,
        }
    }

    /// Deep-clone all items. External callers cannot mutate the internal store.
    pub fn items(&self) -> Vec<ModelMessage> {
        self.items.clone()
    }

    pub fn token_count(&self) -> usize {
        self.token_count
    }

    pub fn version(&self) -> u64 {
        self.version
    }

    pub fn limit(&self) -> usize {
        self.context_limit
    }

    /// Return a snapshot of the current item count and version.
    pub fn snapshot(&self) -> ContextSnapshot {
        ContextSnapshot {
            index: self.items.len(),
            version: self.version,
        }
    }

    /// Return a reference to items starting at index `idx`.
    pub fn items_since(&self, idx: usize) -> &[ModelMessage] {
        if idx >= self.items.len() {
            &[]
        } else {
            &self.items[idx..]
        }
    }

    /// Append a message (after preparing/truncating tool results) and update
    /// the token count.
    pub fn append(&mut self, msg: ModelMessage) {
        let prepared = Self::prepare(msg);
        let tokens = Self::estimate_tokens(&prepared);
        self.items.push(prepared);
        self.token_count += tokens;
        self.version += 1;
    }

    /// Roll back to the given item count, discarding later items.
    /// No-op when `idx` is past the end.
    pub fn rollback(&mut self, idx: usize) {
        if idx > self.items.len() {
            return;
        }
        let removed: usize = self.items[idx..].iter().map(Self::estimate_tokens).sum();
        self.items.truncate(idx);
        self.token_count = self.token_count.saturating_sub(removed);
        self.version += 1;
    }

    /// Replace items in `[start..end)` with `new` messages.
    pub fn replace_range(&mut self, start: usize, end: usize, new: Vec<ModelMessage>) {
        let removed: usize = self.items[start..end]
            .iter()
            .map(Self::estimate_tokens)
            .sum();
        let prepared: Vec<ModelMessage> = new.into_iter().map(Self::prepare).collect();
        let added: usize = prepared.iter().map(Self::estimate_tokens).sum();
        self.items.splice(start..end, prepared);
        self.token_count = self.token_count.saturating_sub(removed) + added;
        self.version += 1;
    }

    // ------------------------------------------------------------------
    // Internal helpers
    // ------------------------------------------------------------------

    /// Truncate `ToolReturn` content that exceeds `MAX_TOOL_RESULT_CHARS`.
    /// Non-`ToolReturn` parts are left untouched.
    fn prepare(msg: ModelMessage) -> ModelMessage {
        let parts: Vec<MessagePart> = msg
            .parts
            .into_iter()
            .map(|part| {
                if let MessagePart::ToolReturn {
                    content,
                    tool_call_id,
                    tool_name,
                } = part
                {
                    if content.len() > MAX_TOOL_RESULT_CHARS {
                        let budget = MAX_TOOL_RESULT_CHARS - 50;
                        let head = budget / 2;
                        let tail = budget - head;
                        let truncated = format!(
                            "{}...\n...[TRUNCATED]...\n{}",
                            &content[..head],
                            &content[content.len() - tail..]
                        );
                        MessagePart::ToolReturn {
                            content: truncated,
                            tool_call_id,
                            tool_name,
                        }
                    } else {
                        MessagePart::ToolReturn {
                            content,
                            tool_call_id,
                            tool_name,
                        }
                    }
                } else {
                    part
                }
            })
            .collect();
        ModelMessage { parts, ..msg }
    }

    /// Rough token estimate: `(total chars of all part payloads) / 4`, minimum 1.
    /// This aligns with the Python `char_count / 4` heuristic.
    pub fn estimate_tokens(msg: &ModelMessage) -> usize {
        let total: usize = msg
            .parts
            .iter()
            .map(|part| match part {
                MessagePart::SystemPrompt { content }
                | MessagePart::UserPrompt { content }
                | MessagePart::Text { content } => content.len(),
                MessagePart::ToolCall { arguments, .. } => arguments.len(),
                MessagePart::ToolReturn { content, .. } => content.len(),
            })
            .sum();
        (total / 4).max(1)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::message::MessageRole;

    fn user_msg(content: &str) -> ModelMessage {
        ModelMessage {
            role: MessageRole::User,
            parts: vec![MessagePart::UserPrompt {
                content: content.into(),
            }],
        }
    }

    fn assistant_text(content: &str) -> ModelMessage {
        ModelMessage {
            role: MessageRole::Assistant,
            parts: vec![MessagePart::Text {
                content: content.into(),
            }],
        }
    }

    fn tool_msg(content: &str) -> ModelMessage {
        ModelMessage {
            role: MessageRole::Tool,
            parts: vec![MessagePart::ToolReturn {
                tool_call_id: "call_1".into(),
                tool_name: "test_tool".into(),
                content: content.into(),
            }],
        }
    }

    fn assistant_with_tool_calls() -> ModelMessage {
        ModelMessage {
            role: MessageRole::Assistant,
            parts: vec![
                MessagePart::Text {
                    content: "calling tools".into(),
                },
                MessagePart::ToolCall {
                    tool_call_id: "call_1".into(),
                    tool_name: "search".into(),
                    arguments: r#"{"q":"test"}"#.into(),
                },
            ],
        }
    }

    #[test]
    fn test_new() {
        let ctx = ContextManager::new(100);
        assert!(ctx.items().is_empty());
        assert_eq!(ctx.token_count(), 0);
        assert_eq!(ctx.version(), 0);
        assert_eq!(ctx.limit(), 100);
    }

    #[test]
    fn test_append() {
        let mut ctx = ContextManager::new(100);
        ctx.append(user_msg("hello"));
        assert_eq!(ctx.items().len(), 1);
        assert_eq!(ctx.version(), 1);
        assert!(ctx.token_count() > 0);
        assert_eq!(
            ctx.items()[0].parts[0],
            MessagePart::UserPrompt {
                content: "hello".into()
            }
        );
    }

    #[test]
    fn test_snapshot_and_items_since() {
        let mut ctx = ContextManager::new(100);
        assert_eq!(
            ctx.snapshot(),
            ContextSnapshot {
                index: 0,
                version: 0
            }
        );
        ctx.append(user_msg("a"));
        ctx.append(user_msg("b"));
        let snap = ctx.snapshot();
        assert_eq!(snap.index, 2);
        assert_eq!(snap.version, 2);

        let since = ctx.items_since(1);
        assert_eq!(since.len(), 1);
        assert_eq!(
            since[0].parts[0],
            MessagePart::UserPrompt {
                content: "b".into()
            }
        );

        let empty = ctx.items_since(5);
        assert!(empty.is_empty());
    }

    #[test]
    fn test_rollback() {
        let mut ctx = ContextManager::new(100);
        ctx.append(user_msg("a"));
        ctx.append(user_msg("b"));
        ctx.append(user_msg("c"));
        let before = ctx.token_count();
        ctx.rollback(1);
        assert_eq!(ctx.items().len(), 1);
        assert_eq!(
            ctx.items()[0].parts[0],
            MessagePart::UserPrompt {
                content: "a".into()
            }
        );
        assert!(ctx.token_count() < before);
        assert_eq!(ctx.version(), 4);
    }

    #[test]
    fn test_rollback_past_end() {
        let mut ctx = ContextManager::new(100);
        ctx.append(user_msg("a"));
        let ver = ctx.version();
        ctx.rollback(10);
        assert_eq!(ctx.items().len(), 1);
        assert_eq!(ctx.version(), ver);
    }

    #[test]
    fn test_replace_range() {
        let mut ctx = ContextManager::new(100);
        ctx.append(user_msg("a"));
        ctx.append(user_msg("b"));
        ctx.append(user_msg("c"));

        let replacement = vec![user_msg("x"), user_msg("y")];
        ctx.replace_range(1, 3, replacement);
        assert_eq!(ctx.items().len(), 3);
        assert_eq!(
            ctx.items()[0].parts[0],
            MessagePart::UserPrompt {
                content: "a".into()
            }
        );
        assert_eq!(
            ctx.items()[1].parts[0],
            MessagePart::UserPrompt {
                content: "x".into()
            }
        );
        assert_eq!(
            ctx.items()[2].parts[0],
            MessagePart::UserPrompt {
                content: "y".into()
            }
        );
        assert_eq!(ctx.version(), 4);
    }

    #[test]
    fn test_prepare_truncation() {
        let long = "A".repeat(60_000);
        let msg = tool_msg(&long);
        let prepared = ContextManager::prepare(msg);
        // Only the ToolReturn part should be truncated
        let content = match &prepared.parts[0] {
            MessagePart::ToolReturn { content, .. } => content,
            _ => panic!("expected ToolReturn"),
        };
        assert!(content.len() < 60_000);
        assert!(content.contains("[TRUNCATED]"));
    }

    #[test]
    fn test_prepare_short_tool_msg() {
        let short = "short result";
        let msg = tool_msg(short);
        let prepared = ContextManager::prepare(msg);
        let content = match &prepared.parts[0] {
            MessagePart::ToolReturn { content, .. } => content,
            _ => panic!("expected ToolReturn"),
        };
        assert_eq!(content, "short result");
    }

    #[test]
    fn test_prepare_non_tool_untouched() {
        let long = "A".repeat(60_000);
        let msg = user_msg(&long);
        let prepared = ContextManager::prepare(msg);
        let content = match &prepared.parts[0] {
            MessagePart::UserPrompt { content } => content,
            _ => panic!("expected UserPrompt"),
        };
        assert_eq!(content.len(), 60_000);
    }

    #[test]
    fn test_estimate_tokens() {
        let msg = user_msg("hello world");
        // "hello world" = 11 chars / 4 = 2, min 1
        assert_eq!(ContextManager::estimate_tokens(&msg), 2);
    }

    #[test]
    fn test_estimate_tokens_min_one() {
        let msg = user_msg("ab");
        assert_eq!(ContextManager::estimate_tokens(&msg), 1);
    }

    #[test]
    fn test_estimate_tokens_with_tool_calls() {
        let msg = assistant_with_tool_calls();
        // Text "calling tools" = 13 chars
        // arguments "{\"q\":\"test\"}" = 12 chars
        // total = 25, /4 = 6
        assert_eq!(ContextManager::estimate_tokens(&msg), 6);
    }

    #[test]
    fn test_token_invariant_after_append() {
        let mut ctx = ContextManager::new(100);
        ctx.append(user_msg("hello world"));
        let expected: usize = ctx
            .items()
            .iter()
            .map(ContextManager::estimate_tokens)
            .sum();
        assert_eq!(ctx.token_count(), expected);
    }

    #[test]
    fn test_token_invariant_after_replace_range() {
        let mut ctx = ContextManager::new(100);
        ctx.append(user_msg("a"));
        ctx.append(assistant_text("longer text here for testing"));
        ctx.replace_range(0, 2, vec![user_msg("replacement")]);
        let expected: usize = ctx
            .items()
            .iter()
            .map(ContextManager::estimate_tokens)
            .sum();
        assert_eq!(ctx.token_count(), expected);
    }

    #[test]
    fn test_token_invariant_after_rollback() {
        let mut ctx = ContextManager::new(100);
        ctx.append(user_msg("msg1"));
        ctx.append(user_msg("msg2 longer content"));
        ctx.append(user_msg("msg3"));
        ctx.rollback(1);
        let expected: usize = ctx
            .items()
            .iter()
            .map(ContextManager::estimate_tokens)
            .sum();
        assert_eq!(ctx.token_count(), expected);
    }

    #[test]
    fn test_items_deep_clone() {
        let mut ctx = ContextManager::new(100);
        ctx.append(user_msg("original"));
        let cloned = ctx.items();
        // Mutating the clone should not affect the original
        let mut mutated = cloned[0].clone();
        mutated.parts = vec![];
        assert_eq!(ctx.items()[0].parts.len(), 1);
        drop(mutated);
        drop(cloned);
        // Still intact
        assert_eq!(ctx.items().len(), 1);
        assert_eq!(ctx.token_count(), 2);
    }
}
