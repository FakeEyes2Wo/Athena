use crate::context::ContextManager;
use crate::message::{MessagePart, MessageRole, ModelMessage};
use std::{future::Future, pin::Pin};

/// Record produced by a successful compaction.
#[derive(Debug, Clone)]
pub struct Compaction {
    pub version: u64,
    pub summary: String,
    pub original_items: Vec<ModelMessage>,
}

/// Immutable work prepared from a context before an asynchronous summary call.
///
/// The plan owns the messages passed to the summarizer, so callers do not need
/// to borrow or lock the context while awaiting the model.
#[derive(Debug, Clone)]
pub struct CompactionPlan {
    source_version: u64,
    split_index: usize,
    original_items: Vec<ModelMessage>,
}

impl CompactionPlan {
    pub fn source_version(&self) -> u64 {
        self.source_version
    }

    pub fn messages(&self) -> &[ModelMessage] {
        &self.original_items
    }
}

/// Trait for an LLM client used by compaction.
///
/// Defined without `#[async_trait]` for edition-2024 compatibility.
pub trait Summarizer: Send + Sync {
    fn summarize<'a>(
        &'a self,
        messages: &'a [ModelMessage],
    ) -> Pin<Box<dyn Future<Output = Result<String, String>> + Send + 'a>>;
}

/// Compacts conversation context by summarizing older messages.
///
/// The two-phase [`Self::prepare`] / [`Self::commit`] API lets callers await a
/// [`Summarizer`] without borrowing or locking the context. Commit rejects a
/// plan when the context version changed in the meantime.
pub struct Compactor {
    keep_recent_tokens: usize,
    #[allow(dead_code)]
    summary_model: String,
}

impl Compactor {
    pub fn new(keep_recent_tokens: usize, summary_model: String) -> Self {
        Self {
            keep_recent_tokens,
            summary_model,
        }
    }

    /// Returns `true` when `ctx` has reached or exceeded `at_tokens`.
    pub fn should_compact(&self, ctx: &ContextManager, at_tokens: usize) -> bool {
        ctx.token_count() >= at_tokens
    }

    /// Freeze the older context segment and its source version.
    ///
    /// Returns `None` when all messages fall inside the recent-token window.
    pub fn prepare(&self, ctx: &ContextManager) -> Option<CompactionPlan> {
        let items = ctx.items();
        let split_index = self.split_recent(&items);
        let original_items = items[..split_index].to_vec();
        if original_items.is_empty() {
            return None;
        }

        Some(CompactionPlan {
            source_version: ctx.version(),
            split_index,
            original_items,
        })
    }

    /// Summarize a prepared plan without accessing the source context.
    pub async fn summarize(
        &self,
        plan: &CompactionPlan,
        summarizer: &dyn Summarizer,
    ) -> Result<String, String> {
        summarizer.summarize(plan.messages()).await
    }

    /// Commit a prepared summary if the context still matches its source.
    pub fn commit(
        &self,
        ctx: &mut ContextManager,
        plan: CompactionPlan,
        summary: String,
    ) -> Result<Compaction, String> {
        if ctx.version() != plan.source_version {
            return Err("concurrent modification during compaction".into());
        }

        let summary_msg = ModelMessage {
            role: MessageRole::System,
            parts: vec![MessagePart::SystemPrompt {
                content: format!("[HISTORY SUMMARY]\n{}", summary),
            }],
        };
        ctx.replace_range(0, plan.split_index, vec![summary_msg]);
        Ok(Compaction {
            version: ctx.version(),
            summary,
            original_items: plan.original_items,
        })
    }

    /// Convenience path for callers that do not interleave context mutations.
    pub async fn compact(
        &self,
        ctx: &mut ContextManager,
        summarizer: &dyn Summarizer,
    ) -> Result<Compaction, String> {
        let Some(plan) = self.prepare(ctx) else {
            return Ok(Compaction {
                version: ctx.version(),
                summary: String::new(),
                original_items: vec![],
            });
        };

        let summary = self.summarize(&plan, summarizer).await?;
        self.commit(ctx, plan, summary)
    }

    /// Find the split index: messages at or after this index are kept as
    /// recent history. Iterates from the end of `items` until accumulated
    /// tokens reach `keep_recent_tokens`.
    fn split_recent(&self, items: &[ModelMessage]) -> usize {
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
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn user_msg(content: &str) -> ModelMessage {
        ModelMessage {
            role: MessageRole::User,
            parts: vec![MessagePart::UserPrompt {
                content: content.into(),
            }],
        }
    }

    struct TestSummarizer;

    impl Summarizer for TestSummarizer {
        fn summarize<'a>(
            &'a self,
            _messages: &'a [ModelMessage],
        ) -> Pin<Box<dyn Future<Output = Result<String, String>> + Send + 'a>> {
            Box::pin(async move { Ok("unified summary".into()) })
        }
    }

    #[test]
    fn test_compactor_new() {
        let c = Compactor::new(500, "gpt-4".into());
        assert_eq!(c.keep_recent_tokens, 500);
    }

    #[test]
    fn test_should_compact_true() {
        let mut ctx = ContextManager::new(1000);
        ctx.append(user_msg("hello world this is a test"));
        let c = Compactor::new(10, "test".into());
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
        assert_eq!(c.split_recent(&items), 0);
    }

    #[test]
    fn test_split_recent_above_threshold() {
        let items = vec![
            user_msg(&"A".repeat(200)),
            user_msg(&"B".repeat(200)),
            user_msg(&"C".repeat(200)),
        ];
        let c = Compactor::new(80, "test".into());
        // End: C (~50) acc=50<80, B (~50) acc=100>=80 → index of B = 1
        assert_eq!(c.split_recent(&items), 1);
    }

    #[test]
    fn test_split_recent_single_item_exceeds() {
        let items = vec![user_msg(&"A".repeat(1000))];
        let c = Compactor::new(50, "test".into());
        assert_eq!(c.split_recent(&items), 0);
    }

    #[test]
    fn test_compact_success() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
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

            assert_eq!(ctx.items().len(), 2);
            assert_eq!(ctx.items()[0].role, MessageRole::System);
            assert!(matches!(&ctx.items()[0].parts[0],
                MessagePart::SystemPrompt { content } if content.contains("unified summary")));
            assert_eq!(
                ctx.items()[1].parts[0],
                MessagePart::UserPrompt {
                    content: "B".repeat(400)
                }
            );
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
            // No old items — summary is empty, no checkpoint created.
            assert!(compaction.summary.is_empty());
            // Context unchanged.
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

    #[test]
    fn test_version_tracking() {
        // Verify that version increases on every mutation.
        let mut ctx = ContextManager::new(5000);
        assert_eq!(ctx.version(), 0);
        ctx.append(user_msg("hello"));
        assert!(ctx.version() > 0);
        let v1 = ctx.version();
        ctx.append(user_msg("world"));
        assert!(ctx.version() > v1);
    }
}
