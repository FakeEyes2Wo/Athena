use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    System,
    User,
    Assistant,
    Tool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub role: Role,
    pub content: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tool_calls: Vec<ToolCallRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCallRef {
    pub call_id: String,
    pub name: String,
    /// JSON string of arguments.
    pub arguments: String,
}

const MAX_TOOL_RESULT_CHARS: usize = 50_000;

pub struct ContextManager {
    items: Vec<Message>,
    token_count: usize,
    context_limit: usize,
    version: u64,
}

impl ContextManager {
    pub fn new(context_limit: usize) -> Self {
        Self { items: Vec::new(), token_count: 0, context_limit, version: 0 }
    }

    pub fn items(&self) -> &[Message] {
        &self.items
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

    /// Return snapshot of (item_count, version).
    pub fn snapshot(&self) -> (usize, u64) {
        (self.items.len(), self.version)
    }

    /// Return items starting from index `idx`.
    pub fn items_since(&self, idx: usize) -> &[Message] {
        if idx >= self.items.len() {
            &[]
        } else {
            &self.items[idx..]
        }
    }

    /// Append a message and update token count.
    pub fn append(&mut self, msg: Message) {
        let prepared = Self::prepare(msg);
        let tokens = Self::estimate_tokens(&prepared);
        self.items.push(prepared);
        self.token_count += tokens;
        self.version += 1;
    }

    /// Roll back to item count `idx`, discarding later items.
    pub fn rollback(&mut self, idx: usize) {
        if idx > self.items.len() {
            return;
        }
        let removed: usize = self.items[idx..].iter().map(Self::estimate_tokens).sum();
        self.items.truncate(idx);
        self.token_count = self.token_count.saturating_sub(removed);
        self.version += 1;
    }

    /// Replace items in [start..end) with `new` messages.
    pub fn replace_range(&mut self, start: usize, end: usize, new: Vec<Message>) {
        let removed: usize = self.items[start..end].iter().map(Self::estimate_tokens).sum();
        let added: usize = new.iter().map(Self::estimate_tokens).sum();
        let prepared: Vec<Message> = new.into_iter().map(Self::prepare).collect();
        self.items.splice(start..end, prepared);
        self.token_count = self.token_count.saturating_sub(removed) + added;
        self.version += 1;
    }

    /// Truncate tool-result content that exceeds MAX_TOOL_RESULT_CHARS.
    fn prepare(msg: Message) -> Message {
        if msg.role != Role::Tool {
            return msg;
        }
        if msg.content.len() <= MAX_TOOL_RESULT_CHARS {
            return msg;
        }
        let budget = MAX_TOOL_RESULT_CHARS - 50;
        let head = budget / 2;
        let tail = budget - head;
        let truncated = format!(
            "{}...\n...[TRUNCATED]...\n{}",
            &msg.content[..head],
            &msg.content[msg.content.len() - tail..]
        );
        Message { content: truncated, ..msg }
    }

    /// Rough token estimate: (content_len + tool_call_args_len) / 4, minimum 1.
    pub fn estimate_tokens(msg: &Message) -> usize {
        let mut total = msg.content.len();
        for tc in &msg.tool_calls {
            total += tc.arguments.len();
        }
        (total / 4).max(1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn user_msg(content: &str) -> Message {
        Message {
            role: Role::User,
            content: content.to_string(),
            tool_calls: vec![],
            tool_call_id: None,
            tool_name: None,
        }
    }

    fn tool_msg(content: &str) -> Message {
        Message {
            role: Role::Tool,
            content: content.to_string(),
            tool_calls: vec![],
            tool_call_id: Some("call_1".into()),
            tool_name: Some("test_tool".into()),
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
        assert_eq!(ctx.items()[0].content, "hello");
    }

    #[test]
    fn test_snapshot_and_items_since() {
        let mut ctx = ContextManager::new(100);
        assert_eq!(ctx.snapshot(), (0, 0));
        ctx.append(user_msg("a"));
        ctx.append(user_msg("b"));
        let (len, ver) = ctx.snapshot();
        assert_eq!(len, 2);
        assert_eq!(ver, 2);

        let since = ctx.items_since(1);
        assert_eq!(since.len(), 1);
        assert_eq!(since[0].content, "b");

        let since_empty = ctx.items_since(5);
        assert!(since_empty.is_empty());
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
        assert_eq!(ctx.items()[0].content, "a");
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
        assert_eq!(ctx.items()[0].content, "a");
        assert_eq!(ctx.items()[1].content, "x");
        assert_eq!(ctx.items()[2].content, "y");
        assert_eq!(ctx.version(), 4);
    }

    #[test]
    fn test_prepare_truncation() {
        let long = "A".repeat(60_000);
        let msg = tool_msg(&long);
        let prepared = ContextManager::prepare(msg);
        assert!(prepared.content.len() < 60_000);
        assert!(prepared.content.contains("[TRUNCATED]"));
    }

    #[test]
    fn test_prepare_short_tool_msg() {
        let short = "short result";
        let msg = tool_msg(short);
        let prepared = ContextManager::prepare(msg);
        assert_eq!(prepared.content, "short result");
    }

    #[test]
    fn test_prepare_non_tool_untouched() {
        let long = "A".repeat(60_000);
        let msg = user_msg(&long);
        let prepared = ContextManager::prepare(msg);
        assert_eq!(prepared.content.len(), 60_000);
    }

    #[test]
    fn test_estimate_tokens() {
        let msg = user_msg("hello world");
        let tokens = ContextManager::estimate_tokens(&msg);
        // "hello world" = 11 chars / 4 = 2, min 1
        assert_eq!(tokens, 2);
    }

    #[test]
    fn test_estimate_tokens_min_one() {
        let msg = user_msg("ab");
        let tokens = ContextManager::estimate_tokens(&msg);
        assert_eq!(tokens, 1);
    }

    #[test]
    fn test_estimate_tokens_with_tool_calls() {
        let msg = Message {
            role: Role::Assistant,
            content: "call".into(),
            tool_calls: vec![ToolCallRef {
                call_id: "1".into(),
                name: "get".into(),
                arguments: r#"{"url":"http://example.com"}"#.into(),
            }],
            tool_call_id: None,
            tool_name: None,
        };
        let tokens = ContextManager::estimate_tokens(&msg);
        // content=4, args=30, total=34, /4=8
        assert_eq!(tokens, 8);
    }

    #[test]
    fn test_serialize_role() {
        let json = serde_json::to_string(&Role::User).unwrap();
        assert_eq!(json, "\"user\"");
        let json = serde_json::to_string(&Role::Assistant).unwrap();
        assert_eq!(json, "\"assistant\"");
        let json = serde_json::to_string(&Role::System).unwrap();
        assert_eq!(json, "\"system\"");
        let json = serde_json::to_string(&Role::Tool).unwrap();
        assert_eq!(json, "\"tool\"");
    }

    #[test]
    fn test_role_partial_eq() {
        assert_eq!(Role::User, Role::User);
        assert_ne!(Role::User, Role::Assistant);
    }

    #[test]
    fn test_message_roundtrip() {
        let msg = Message {
            role: Role::Assistant,
            content: "Hello!".into(),
            tool_calls: vec![ToolCallRef {
                call_id: "call_abc".into(),
                name: "get_weather".into(),
                arguments: r#"{"city":"Tokyo"}"#.into(),
            }],
            tool_call_id: None,
            tool_name: None,
        };
        let json = serde_json::to_string(&msg).unwrap();
        let back: Message = serde_json::from_str(&json).unwrap();
        assert_eq!(back.role, Role::Assistant);
        assert_eq!(back.content, "Hello!");
        assert_eq!(back.tool_calls.len(), 1);
        assert_eq!(back.tool_calls[0].name, "get_weather");
    }

    #[test]
    fn test_message_deserialize_defaults() {
        let json = r#"{"role":"user","content":"hi"}"#;
        let msg: Message = serde_json::from_str(json).unwrap();
        assert!(msg.tool_calls.is_empty());
        assert!(msg.tool_call_id.is_none());
        assert!(msg.tool_name.is_none());
    }
}
