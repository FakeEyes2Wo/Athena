//! Integration tests for rollout recovery — write messages + checkpoint,
//! then recover and verify the context matches.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::path::PathBuf;

use athena_memory::{ContextManager, MessagePart, MessageRole, ModelMessage, RolloutRecorder};

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

fn test_dir() -> PathBuf {
    std::env::temp_dir().join(format!("rollout-int-{}", uuid::Uuid::new_v4()))
}

// ---------------------------------------------------------------------------
// Basic write + recovery
// ---------------------------------------------------------------------------

#[test]
fn test_write_messages_then_recover() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let dir = test_dir();
        let mut recorder = RolloutRecorder::new(dir.clone());
        let path = recorder.open("test-thread").await.unwrap();
        recorder.record(&user_msg("hello")).await.unwrap();
        recorder.record(&user_msg("world")).await.unwrap();
        recorder.close().await.unwrap();

        let ctx = RolloutRecorder::resume_context(&path, 10_000)
            .await
            .unwrap();
        assert_eq!(ctx.items().len(), 2);
        assert_eq!(
            ctx.items()[0].parts[0],
            MessagePart::UserPrompt {
                content: "hello".into()
            }
        );
        assert_eq!(
            ctx.items()[1].parts[0],
            MessagePart::UserPrompt {
                content: "world".into()
            }
        );

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir_all(&dir);
    });
}

// ---------------------------------------------------------------------------
// Compaction checkpoint resets state during recovery
// ---------------------------------------------------------------------------

#[test]
fn test_recovery_with_compaction() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let dir = test_dir();
        let mut recorder = RolloutRecorder::new(dir.clone());
        let path = recorder.open("thread-1").await.unwrap();
        recorder.record(&user_msg("old msg 1")).await.unwrap();
        recorder.record(&user_msg("old msg 2")).await.unwrap();
        recorder
            .record_compaction(2, "compacted two old messages")
            .await
            .unwrap();
        recorder.record(&user_msg("new msg 1")).await.unwrap();
        recorder.record(&user_msg("new msg 2")).await.unwrap();
        recorder.close().await.unwrap();

        let ctx = RolloutRecorder::resume_context(&path, 10_000)
            .await
            .unwrap();
        assert_eq!(ctx.items().len(), 3);

        assert_eq!(ctx.items()[0].role, MessageRole::System);
        let summary_content = match &ctx.items()[0].parts[0] {
            MessagePart::SystemPrompt { content } => content.clone(),
            _ => panic!("expected SystemPrompt"),
        };
        assert!(
            summary_content.contains("compacted two old messages"),
            "summary should contain checkpoint text, got: {}",
            summary_content
        );

        assert_eq!(
            ctx.items()[1].parts[0],
            MessagePart::UserPrompt {
                content: "new msg 1".into()
            }
        );
        assert_eq!(
            ctx.items()[2].parts[0],
            MessagePart::UserPrompt {
                content: "new msg 2".into()
            }
        );

        let expected: usize = ctx
            .items()
            .iter()
            .map(ContextManager::estimate_tokens)
            .sum();
        assert_eq!(ctx.token_count(), expected);

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir_all(&dir);
    });
}

// ---------------------------------------------------------------------------
// Multiple compaction checkpoints
// ---------------------------------------------------------------------------

#[test]
fn test_recovery_with_multiple_compactions() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let dir = test_dir();
        let mut recorder = RolloutRecorder::new(dir.clone());
        let path = recorder.open("thread-1").await.unwrap();
        recorder.record(&user_msg("msg A")).await.unwrap();
        recorder
            .record_compaction(1, "first compaction")
            .await
            .unwrap();
        recorder.record(&user_msg("msg B")).await.unwrap();
        recorder.record(&user_msg("msg C")).await.unwrap();
        recorder
            .record_compaction(3, "second compaction")
            .await
            .unwrap();
        recorder.record(&user_msg("msg D")).await.unwrap();
        recorder.close().await.unwrap();

        let ctx = RolloutRecorder::resume_context(&path, 10_000)
            .await
            .unwrap();
        assert_eq!(ctx.items().len(), 2);

        let summary = match &ctx.items()[0].parts[0] {
            MessagePart::SystemPrompt { content } => content.clone(),
            _ => panic!("expected SystemPrompt"),
        };
        assert!(
            summary.contains("second compaction"),
            "should use latest compaction, got: {}",
            summary
        );
        assert_eq!(
            ctx.items()[1].parts[0],
            MessagePart::UserPrompt {
                content: "msg D".into()
            }
        );

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir_all(&dir);
    });
}

// ---------------------------------------------------------------------------
// Torn last record
// ---------------------------------------------------------------------------

#[test]
fn test_recovery_skips_torn_last_record() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let dir = test_dir();
        let mut recorder = RolloutRecorder::new(dir.clone());
        let path = recorder.open("test").await.unwrap();
        recorder.record(&user_msg("valid message")).await.unwrap();
        recorder.close().await.unwrap();

        // Manually append a torn line
        {
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new()
                .append(true)
                .open(&path)
                .unwrap();
            writeln!(f, "{{\"seq\": 1, \"ts\": \"...\", \"msg\": {{").unwrap();
        }

        let ctx = RolloutRecorder::resume_context(&path, 10_000)
            .await
            .unwrap();
        assert_eq!(ctx.items().len(), 1);
        assert_eq!(
            ctx.items()[0].parts[0],
            MessagePart::UserPrompt {
                content: "valid message".into()
            }
        );

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir_all(&dir);
    });
}

// ---------------------------------------------------------------------------
// Corrupt middle record (rewrite file with corruption)
// ---------------------------------------------------------------------------

#[test]
fn test_recovery_skips_corrupt_middle_record() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let dir = test_dir();
        let mut recorder = RolloutRecorder::new(dir.clone());
        let path = recorder.open("test").await.unwrap();
        recorder.record(&user_msg("first")).await.unwrap();
        recorder.record(&user_msg("third")).await.unwrap();
        recorder.close().await.unwrap();

        // Overwrite with corrupt middle
        {
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new()
                .write(true)
                .truncate(true)
                .open(&path)
                .unwrap();
            let msg1 = serde_json::json!({
                "seq": 0,
                "ts": "2026-07-25T12:00:00+00:00",
                "msg": {"role": "user", "parts": [{"part_kind": "user-prompt", "content": "first"}]}
            });
            writeln!(f, "{}", serde_json::to_string(&msg1).unwrap()).unwrap();
            writeln!(f, "corrupt line that is not valid json").unwrap();
            let msg3 = serde_json::json!({
                "seq": 2,
                "ts": "2026-07-25T12:00:00+00:00",
                "msg": {"role": "user", "parts": [{"part_kind": "user-prompt", "content": "third"}]}
            });
            writeln!(f, "{}", serde_json::to_string(&msg3).unwrap()).unwrap();
        }

        let ctx = RolloutRecorder::resume_context(&path, 10_000)
            .await
            .unwrap();
        assert_eq!(ctx.items().len(), 2);
        assert_eq!(
            ctx.items()[0].parts[0],
            MessagePart::UserPrompt {
                content: "first".into()
            }
        );
        assert_eq!(
            ctx.items()[1].parts[0],
            MessagePart::UserPrompt {
                content: "third".into()
            }
        );

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir_all(&dir);
    });
}

// ---------------------------------------------------------------------------
// Empty file
// ---------------------------------------------------------------------------

#[test]
fn test_recovery_from_empty_file() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let dir = test_dir();
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("empty.jsonl");
        std::fs::write(&path, "").unwrap();

        let ctx = RolloutRecorder::resume_context(&path, 10_000)
            .await
            .unwrap();
        assert!(ctx.items().is_empty());
        assert_eq!(ctx.token_count(), 0);

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir_all(&dir);
    });
}

// ---------------------------------------------------------------------------
// Token invariant after recovery
// ---------------------------------------------------------------------------

#[test]
fn test_token_invariant_after_recovery() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let dir = test_dir();
        let mut recorder = RolloutRecorder::new(dir.clone());
        let path = recorder.open("test").await.unwrap();
        recorder.record(&user_msg("hello world")).await.unwrap();
        recorder
            .record(&user_msg("longer message for token testing"))
            .await
            .unwrap();
        recorder.close().await.unwrap();

        let ctx = RolloutRecorder::resume_context(&path, 10_000)
            .await
            .unwrap();
        let expected: usize = ctx
            .items()
            .iter()
            .map(ContextManager::estimate_tokens)
            .sum();
        assert_eq!(
            ctx.token_count(),
            expected,
            "token invariant violated after recovery"
        );

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir_all(&dir);
    });
}

// ---------------------------------------------------------------------------
// Record without open is no-op
// ---------------------------------------------------------------------------

#[test]
fn test_record_without_open_is_noop() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    rt.block_on(async {
        let mut recorder = RolloutRecorder::new(PathBuf::from("/tmp"));
        assert!(recorder.path().is_none());
        recorder
            .record(&user_msg("should not crash"))
            .await
            .unwrap();
        recorder.record_compaction(1, "noop").await.unwrap();
        recorder.close().await.unwrap();
    });
}
