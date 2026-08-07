use athena_types::{ArtifactRef, ThreadId, TurnId};
use serde::{Deserialize, Serialize};

// ── Parameter DTOs ──

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThreadStartParams {
    pub session_id: String,
    pub context_ref: ArtifactRef,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TurnStartParams {
    pub thread_id: ThreadId,
    pub request_ref: ArtifactRef,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TurnInterruptParams {
    pub thread_id: ThreadId,
    pub turn_id: TurnId,
    #[serde(default = "default_interrupt_reason")]
    pub reason: String,
}

fn default_interrupt_reason() -> String {
    "user_requested".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThreadForkParams {
    pub thread_id: ThreadId,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub after_turn_id: Option<TurnId>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThreadSubscribeParams {
    pub thread_id: ThreadId,
    #[serde(default)]
    pub after_sequence: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThreadUnsubscribeParams {
    pub subscription_id: String,
}

// ── Result DTOs ──

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThreadStartedResult {
    pub thread_id: ThreadId,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TurnStartedResult {
    pub turn_id: TurnId,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThreadForkedResult {
    pub thread_id: ThreadId,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubscribedResult {
    pub subscription_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InterruptedResult {
    pub turn_id: TurnId,
    #[serde(default = "default_interrupted_status")]
    pub status: String,
}

fn default_interrupted_status() -> String {
    "interrupted".into()
}
