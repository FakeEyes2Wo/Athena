use crate::event::EventDraft;
use athena_types::{ArtifactRef, AthenaThread, AthenaTurn};
use std::sync::Arc;
use tokio_util::sync::CancellationToken;

/// Error returned when emitting an event for a turn that is no longer active.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmitError;

impl std::fmt::Display for EmitError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "turn is no longer active")
    }
}

impl std::error::Error for EmitError {}

/// A sink for non-terminal turn events. The thread actor rejects emits once the
/// turn has reached a terminal state (terminal events are the actor's alone).
#[async_trait::async_trait]
pub trait EventSink: Send + Sync {
    async fn emit(&self, draft: EventDraft) -> Result<(), EmitError>;
}

/// Everything a runner needs to execute one turn.
pub struct TurnInput {
    pub thread: AthenaThread,
    pub turn: AthenaTurn,
    pub emit: Arc<dyn EventSink>,
    pub cancel: CancellationToken,
}

/// A successful turn's committed references.
#[derive(Debug, Clone)]
pub struct TurnOutput {
    pub result_ref: ArtifactRef,
    pub next_context_ref: ArtifactRef,
}

/// Error returned by a runner.
#[derive(Debug, Clone)]
pub struct RunnerError(pub String);

impl std::fmt::Display for RunnerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for RunnerError {}

/// Executes a single turn. Implemented by `athena-agent`; the runtime never
/// depends on any concrete agent.
#[async_trait::async_trait]
pub trait TurnRunner: Send + Sync {
    async fn run(&self, input: TurnInput) -> Result<TurnOutput, RunnerError>;
}
