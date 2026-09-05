mod ids;
mod status;

pub use ids::{ArtifactRef, CommitHash, SessionId, ThreadId, TurnId, ValidationError};
pub use status::{ThreadStatus, TurnStatus};

use serde::{Deserialize, Serialize};

/// A conversation thread within a session.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AthenaThread {
    pub thread_id: ThreadId,
    pub session_id: SessionId,
    pub status: ThreadStatus,
    pub context_ref: ArtifactRef,
}

/// A single turn (request/response cycle) within a thread.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AthenaTurn {
    pub turn_id: TurnId,
    pub thread_id: ThreadId,
    pub request_ref: ArtifactRef,
    pub status: TurnStatus,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result_ref: Option<ArtifactRef>,
}
