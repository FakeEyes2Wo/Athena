//! Shared fake runners for runtime tests.
#![allow(dead_code)]

use athena_runtime::{EventDraft, RunnerError, TurnInput, TurnOutput, TurnRunner};
use athena_types::ArtifactRef;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use tokio::sync::Mutex;

/// Emits one progress event, then completes with derived refs.
pub struct CompletingRunner;

#[async_trait::async_trait]
impl TurnRunner for CompletingRunner {
    async fn run(&self, input: TurnInput) -> Result<TurnOutput, RunnerError> {
        let _ = input
            .emit
            .emit(EventDraft::new("progress", "ev:progress"))
            .await;
        Ok(TurnOutput {
            result_ref: ArtifactRef::new(format!("result://{}", input.turn.turn_id.as_str()))
                .map_err(|e| RunnerError(e.to_string()))?,
            next_context_ref: ArtifactRef::new(format!("ctx://{}", input.turn.turn_id.as_str()))
                .map_err(|e| RunnerError(e.to_string()))?,
        })
    }
}

/// Always fails.
pub struct FailingRunner;

#[async_trait::async_trait]
impl TurnRunner for FailingRunner {
    async fn run(&self, _input: TurnInput) -> Result<TurnOutput, RunnerError> {
        Err(RunnerError("boom".into()))
    }
}

/// Blocks until cancelled, recording whether a post-cancel emit is rejected.
pub struct BlockingRunner {
    pub post_cancel_emit_rejected: Arc<Mutex<Option<bool>>>,
}

#[async_trait::async_trait]
impl TurnRunner for BlockingRunner {
    async fn run(&self, input: TurnInput) -> Result<TurnOutput, RunnerError> {
        let _ = input
            .emit
            .emit(EventDraft::new("progress", "ev:progress"))
            .await;
        input.cancel.cancelled().await;
        let rejected = input
            .emit
            .emit(EventDraft::new("late", "ev:late"))
            .await
            .is_err();
        *self.post_cancel_emit_rejected.lock().await = Some(rejected);
        Err(RunnerError("cancelled".into()))
    }
}

/// Completes after yielding a few times, to race against interrupts.
pub struct RacyRunner {
    pub count: Arc<AtomicUsize>,
}

#[async_trait::async_trait]
impl TurnRunner for RacyRunner {
    async fn run(&self, input: TurnInput) -> Result<TurnOutput, RunnerError> {
        for _ in 0..3 {
            tokio::task::yield_now().await;
        }
        self.count.fetch_add(1, Ordering::SeqCst);
        Ok(TurnOutput {
            result_ref: ArtifactRef::new(format!("result://{}", input.turn.turn_id.as_str()))
                .map_err(|e| RunnerError(e.to_string()))?,
            next_context_ref: ArtifactRef::new(format!("ctx://{}", input.turn.turn_id.as_str()))
                .map_err(|e| RunnerError(e.to_string()))?,
        })
    }
}

pub fn is_terminal(kind: &str) -> bool {
    matches!(kind, "turn_completed" | "turn_failed" | "turn_interrupted")
}
