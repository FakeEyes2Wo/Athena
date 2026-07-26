//! Integration tests for Compactor — concurrent-modification guard,
//! empty-context guard, and token-invariant verification.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use athena_memory::{
    Compactor, ContextManager, MessagePart, MessageRole, ModelMessage, Summarizer,
};
use std::{future::Future, pin::Pin};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn user_msg(content: &str) -> ModelMessage {
    ModelMessage {
        role: MessageRole::User,
        parts: vec![MessagePart::UserPrompt {
            content: content.into(),
        }],
    }
}

fn assistant_msg(content: &str) -> ModelMessage {
    ModelMessage {
        role: MessageRole::Assistant,
        parts: vec![MessagePart::Text {
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
        Box::pin(async move { Ok("unified summary of older messages".into()) })
    }
}

// ---------------------------------------------------------------------------
// Concurrent modification between prepare and commit is rejected
// ---------------------------------------------------------------------------

#[test]
fn test_concurrent_modification_rejects_commit() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let mut ctx = ContextManager::new(10_000);
        ctx.append(user_msg(&"A".repeat(400)));
        ctx.append(user_msg(&"B".repeat(400)));

        let compactor = Compactor::new(100, "test".into());
        let plan = compactor.prepare(&ctx).expect("old messages should exist");
        let source_version = plan.source_version();
        let summary = compactor.summarize(&plan, &TestSummarizer).await.unwrap();

        ctx.append(user_msg("arrived while summary was running"));
        assert!(ctx.version() > source_version);

        let result = compactor.commit(&mut ctx, plan, summary);
        assert_eq!(
            result.unwrap_err(),
            "concurrent modification during compaction"
        );
        assert_eq!(
            ctx.items().len(),
            3,
            "failed commit must not mutate context"
        );
    });
}

// ---------------------------------------------------------------------------
// Token invariant: after compaction, tokens == sum(estimate(m))
// ---------------------------------------------------------------------------

#[test]
fn test_token_invariant_after_compaction() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let mut ctx = ContextManager::new(10_000);
        ctx.append(user_msg(&"A".repeat(400)));
        ctx.append(user_msg(&"B".repeat(400)));
        ctx.append(assistant_msg(&"C".repeat(400)));

        let compactor = Compactor::new(100, "test".into());
        let summarizer = TestSummarizer;

        let result = compactor.compact(&mut ctx, &summarizer).await;
        assert!(result.is_ok());

        // Token count must equal sum of estimate_tokens for all items
        let expected: usize = ctx
            .items()
            .iter()
            .map(ContextManager::estimate_tokens)
            .sum();
        assert_eq!(
            ctx.token_count(),
            expected,
            "token invariant violated after compaction"
        );
    });
}

// ---------------------------------------------------------------------------
// Version monotonicity across compactions
// ---------------------------------------------------------------------------

#[test]
fn test_version_increases_after_compaction() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let mut ctx = ContextManager::new(10_000);
        ctx.append(user_msg(&"A".repeat(400))); // ~100 tokens
        ctx.append(user_msg(&"B".repeat(200))); // ~50 tokens
        let v_before = ctx.version();

        let compactor = Compactor::new(50, "test".into());
        let summarizer = TestSummarizer;

        let result = compactor.compact(&mut ctx, &summarizer).await;
        assert!(result.is_ok());
        assert!(
            ctx.version() > v_before,
            "version should increase after compaction"
        );
    });
}

// ---------------------------------------------------------------------------
// Multiple compactions in sequence (version monotonicity per call)
// ---------------------------------------------------------------------------

#[test]
fn test_sequential_compactions() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let mut ctx = ContextManager::new(100_000);
        // Fill with many messages
        for i in 0..20 {
            ctx.append(user_msg(&format!("message {}", i).repeat(200)));
        }

        let compactor = Compactor::new(500, "test".into());
        let summarizer = TestSummarizer;

        // First compaction
        let c1 = compactor.compact(&mut ctx, &summarizer).await.unwrap();
        assert!(c1.version > 0);

        // Add more messages and compact again
        for i in 0..5 {
            ctx.append(user_msg(&format!("new msg {}", i).repeat(100)));
        }
        let c2 = compactor.compact(&mut ctx, &summarizer).await.unwrap();
        assert!(
            c2.version > c1.version,
            "second compaction version should be higher"
        );

        // Token invariant holds
        let expected: usize = ctx
            .items()
            .iter()
            .map(ContextManager::estimate_tokens)
            .sum();
        assert_eq!(ctx.token_count(), expected);
    });
}

// ---------------------------------------------------------------------------
// No checkpoint when old messages are empty
// ---------------------------------------------------------------------------

#[test]
fn test_no_checkpoint_on_empty_old() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let mut ctx = ContextManager::new(100);
        // All items are within the "recent" threshold — nothing to compact
        ctx.append(user_msg("just one message"));

        let compactor = Compactor::new(1000, "test".into());
        let summarizer = TestSummarizer;

        let compaction = compactor.compact(&mut ctx, &summarizer).await.unwrap();
        // Summary should be empty (no old messages were compacted)
        assert!(
            compaction.summary.is_empty(),
            "no old items should produce empty summary"
        );
        // Context unchanged — still 1 message
        assert_eq!(ctx.items().len(), 1);
        // original_items should be empty
        assert!(compaction.original_items.is_empty());
    });
}

// ---------------------------------------------------------------------------
// Empty context also returns no checkpoint
// ---------------------------------------------------------------------------

#[test]
fn test_no_checkpoint_on_empty_context() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let mut ctx = ContextManager::new(100);
        let compactor = Compactor::new(10, "test".into());
        let summarizer = TestSummarizer;

        let compaction = compactor.compact(&mut ctx, &summarizer).await.unwrap();
        assert!(compaction.summary.is_empty());
        assert!(ctx.items().is_empty());
    });
}

// ---------------------------------------------------------------------------
// Compaction preserves recent messages
// ---------------------------------------------------------------------------

#[test]
fn test_compaction_preserves_recent_messages() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let mut ctx = ContextManager::new(10_000);
        ctx.append(user_msg(&"A".repeat(400))); // ~100 tokens
        ctx.append(user_msg(&"B".repeat(400))); // ~100 tokens
        ctx.append(assistant_msg(&"C".repeat(200))); // ~50 tokens

        // keep_recent_tokens=120: should keep last message (C, ~50 tokens)
        // and part of B (~100 tokens, total ~150 >= 120)
        // So B and C are recent, A is old.
        let compactor = Compactor::new(120, "test".into());
        let summarizer = TestSummarizer;

        let compaction = compactor.compact(&mut ctx, &summarizer).await.unwrap();

        // Context: [summary, msg_B, msg_C]
        assert_eq!(ctx.items().len(), 3);
        assert_eq!(ctx.items()[0].role, MessageRole::System);

        // B and C should still be present
        assert_eq!(
            ctx.items()[1].parts[0],
            MessagePart::UserPrompt {
                content: "B".repeat(400)
            }
        );
        assert_eq!(
            ctx.items()[2].parts[0],
            MessagePart::Text {
                content: "C".repeat(200)
            }
        );

        // Compaction record should contain the original old messages
        assert_eq!(compaction.original_items.len(), 1);
        assert_eq!(
            compaction.original_items[0].parts[0],
            MessagePart::UserPrompt {
                content: "A".repeat(400)
            }
        );
    });
}

// ---------------------------------------------------------------------------
// Version monotonicity
// ---------------------------------------------------------------------------

#[test]
fn test_version_monotonic_after_compaction() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let mut ctx = ContextManager::new(10_000);
        ctx.append(user_msg(&"A".repeat(400))); // ~100 tokens
        ctx.append(user_msg(&"B".repeat(80))); // ~20 tokens
        let v1 = ctx.version();

        // keep_recent_tokens=10 means B (20 tokens) alone exceeds threshold
        // split = 1 (B is recent), old = [A]
        let compactor = Compactor::new(10, "test".into());
        let summarizer = TestSummarizer;

        let compaction = compactor.compact(&mut ctx, &summarizer).await.unwrap();
        assert!(
            compaction.version > v1,
            "version should increase after compaction"
        );
    });
}
