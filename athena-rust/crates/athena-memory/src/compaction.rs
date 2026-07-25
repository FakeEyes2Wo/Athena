use super::context::{ContextManager, Message, Role};
use std::{future::Future, pin::Pin};

#[derive(Debug, Clone)]
pub struct Compaction {
    pub version: u64,
    pub summary: String,
    pub original_items: Vec<Message>,
}

/// Trait for an LLM client used by compaction.
///
/// Defined without `#[async_trait]` for edition-2024 compatibility.
pub trait Summarizer: Send + Sync {
    fn summarize<'a>(
        &'a self,
        messages: &'a [Message],
    ) -> Pin<Box<dyn Future<Output = Result<String, String>> + Send + 'a>>;
}

pub struct Compactor {
    keep_recent_tokens: usize,
    summary_model: String,
}

impl Compactor {
    pub fn new(keep_recent_tokens: usize, summary_model: String) -> Self {
        Self { keep_recent_tokens, summary_model }
    }

    pub fn should_compact(&self, ctx: &ContextManager, at_tokens: usize) -> bool {
        ctx.token_count() >= at_tokens
    }

    /// Compact context: summarize older messages, keep recent ones intact.
    ///
    /// Returns a `Compaction` record on success. Returns an error if the context
    /// was modified concurrently.
    pub async fn compact(
        &self,
        ctx: &mut ContextManager,
        summarizer: &dyn Summarizer,
    ) -> Result<Compaction, String> {
        let items = ctx.items().to_vec();
        let split = self.split_recent(&items);
        let old = &items[..split];
        if old.is_empty() {
            return Ok(Compaction {
                version: ctx.version(),
                summary: String::new(),
                original_items: vec![],
            });
        }

        let source_version = ctx.version();
        let summary = summarizer.summarize(old).await?;
        if ctx.version() != source_version {
            return Err("concurrent modification during compaction".into());
        }

        let summary_msg = Message {
            role: Role::System,
            content: format!("[HISTORY SUMMARY]\n{}", summary),
            tool_calls: vec![],
            tool_call_id: None,
            tool_name: None,
        };
        ctx.replace_range(0, split, vec![summary_msg]);
        Ok(Compaction {
            version: ctx.version(),
            summary,
            original_items: old.to_vec(),
        })
    }

    /// Find the split index: messages at or after this index should be kept
    /// as recent history. Iterates from the end of `items` until accumulated
    /// tokens reach `keep_recent_tokens`.
    fn split_recent(&self, items: &[Message]) -> usize {
        let mut acc = 0usize;
        for i in (0..items.len()).rev() {
            acc += ContextManager::estimate_tokens(&items[i]);
            if acc >= self.keep_recent_tokens {
                return i;
            }
        }
        0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn user_msg(content: &str) -> Message {
        Message {
            role: Role::User,
            content: content.into(),
            tool_calls: vec![],
            tool_call_id: None,
            tool_name: None,
        }
    }

    struct TestSummarizer;

    impl Summarizer for TestSummarizer {
        fn summarize<'a>(
            &'a self,
            _messages: &'a [Message],
        ) -> Pin<Box<dyn Future<Output = Result<String, String>> + Send + 'a>> {
            Box::pin(async move { Ok("unified summary".into()) })
        }
    }

    #[test]
    fn test_compactor_new() {
        let c = Compactor::new(500, "gpt-4".into());
        assert_eq!(c.keep_recent_tokens, 500);
        assert_eq!(c.summary_model, "gpt-4");
    }

    #[test]
    fn test_should_compact_true() {
        let mut ctx = ContextManager::new(1000);
        ctx.append(user_msg("hello world this is a test"));
        let c = Compactor::new(10, "test".into());
        // Total tokens should be >= 1, so compact at 1
        assert!(c.should_compact(&ctx, 1));
    }

    #[test]
    fn test_should_compact_false() {
        let ctx = ContextManager::new(1000);
        let c = Compactor::new(10, "test".into());
        assert!(!c.should_compact(&ctx, 1));
    }

    #[test]
    fn test_split_recent_all_below_threshold() {
        let items = vec![user_msg("hi"), user_msg("there")];
        let c = Compactor::new(1000, "test".into());
        // Both messages together are below 1000 tokens, so split=0 (all recent)
        assert_eq!(c.split_recent(&items), 0);
    }

    #[test]
    fn test_split_recent_above_threshold() {
        let items = vec![
            user_msg(&"A".repeat(200)),   // ~50 tokens
            user_msg(&"B".repeat(200)),   // ~50 tokens
            user_msg(&"C".repeat(200)),   // ~50 tokens
        ];
        let c = Compactor::new(80, "test".into());
        // Working from end: C (50) acc=50<80, B (50) acc=100>=80, return index of B = 1
        assert_eq!(c.split_recent(&items), 1);
    }

    #[test]
    fn test_split_recent_single_item_exceeds() {
        let items = vec![user_msg(&"A".repeat(1000))];
        let c = Compactor::new(50, "test".into());
        // Single item with ~250 tokens, threshold 50: acc=250>=50, return 0
        assert_eq!(c.split_recent(&items), 0);
    }

    #[test]
    fn test_compact_success() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            // Each message ~400 chars ≈ 100 tokens. keep_recent_tokens=100 means
            // only the last item (from the end, first to cross threshold) is "recent".
            // The older items get summarized.
            let mut ctx = ContextManager::new(5000);
            ctx.append(user_msg(&"A".repeat(400)));
            ctx.append(user_msg(&"B".repeat(400)));

            let compactor = Compactor::new(100, "test".into());
            let summarizer = TestSummarizer;

            let result = compactor.compact(&mut ctx, &summarizer).await;
            assert!(result.is_ok());

            let compaction = result.unwrap();
            assert!(compaction.summary.contains("unified summary"));
            assert!(compaction.version > 0);

            // Old item (A) was replaced by summary message; B is recent and kept.
            assert_eq!(ctx.items().len(), 2);
            assert_eq!(ctx.items()[0].role, Role::System);
            assert!(ctx.items()[0].content.contains("unified summary"));
            assert_eq!(ctx.items()[1].content, "B".repeat(400));
        });
    }

    #[test]
    fn test_compact_empty_old() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let mut ctx = ContextManager::new(100);
            ctx.append(user_msg("recent"));
            let compactor = Compactor::new(1000, "test".into());
            let summarizer = TestSummarizer;

            let result = compactor.compact(&mut ctx, &summarizer).await;
            assert!(result.is_ok());
            let compaction = result.unwrap();
            // No old items to compact — summary is empty
            assert!(compaction.summary.is_empty());
            // Context unchanged
            assert_eq!(ctx.items().len(), 1);
        });
    }

    #[test]
    fn test_compact_no_items() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let mut ctx = ContextManager::new(100);
            let compactor = Compactor::new(10, "test".into());
            let summarizer = TestSummarizer;

            let result = compactor.compact(&mut ctx, &summarizer).await;
            assert!(result.is_ok());
            let compaction = result.unwrap();
            assert!(compaction.summary.is_empty());
            assert!(ctx.items().is_empty());
        });
    }
}
