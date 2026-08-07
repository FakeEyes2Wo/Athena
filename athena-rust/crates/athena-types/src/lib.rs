pub mod domain;
pub mod ids;
pub mod status;

pub use domain::{
    DataCard, ExperimentPlan, Hypothesis, HypothesisStatus, MetricDirection, MetricSpec,
    TaskMetaData,
};
pub use ids::{
    ArtifactRef, CommitHash, NonBlankString, SessionId, ThreadId, TurnId, ValidationError,
};
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

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn test_serialize_thread_roundtrip() {
        let t = AthenaThread {
            thread_id: ThreadId::new("abc").unwrap(),
            session_id: SessionId::new("sess-1").unwrap(),
            status: ThreadStatus::Idle,
            context_ref: ArtifactRef::new("ctx://init").unwrap(),
        };
        let json = serde_json::to_string(&t).unwrap();
        let back: AthenaThread = serde_json::from_str(&json).unwrap();
        assert_eq!(back.thread_id.as_str(), "abc");
        assert_eq!(back.status, ThreadStatus::Idle);
    }

    #[test]
    fn test_deserialize_unknown_field_is_denied() {
        let json = r#"{"thread_id":"t1","session_id":"s1","status":"idle","context_ref":"ctx://1","extra_field":true}"#;
        let err = serde_json::from_str::<AthenaThread>(json).unwrap_err();
        assert!(err.to_string().contains("extra_field"));
    }

    #[test]
    fn test_artifact_ref_rejects_blank() {
        let err = ArtifactRef::new("").unwrap_err();
        assert!(err.to_string().contains("empty or blank"));

        let err = ArtifactRef::new("   ").unwrap_err();
        assert!(err.to_string().contains("empty or blank"));
    }

    #[test]
    fn test_artifact_ref_accepts_valid() {
        let r = ArtifactRef::new("artifact://foo").unwrap();
        assert_eq!(r.as_str(), "artifact://foo");
    }

    #[test]
    fn test_thread_status_serialize() {
        assert_eq!(
            serde_json::to_value(ThreadStatus::Running).unwrap(),
            "running"
        );
        assert_eq!(
            serde_json::to_value(ThreadStatus::Closed).unwrap(),
            "closed"
        );
    }

    #[test]
    fn test_turn_status_serialize() {
        assert_eq!(
            serde_json::to_value(TurnStatus::Interrupted).unwrap(),
            "interrupted"
        );
        assert_eq!(
            serde_json::to_value(TurnStatus::Completed).unwrap(),
            "completed"
        );
    }

    #[test]
    fn test_hypothesis_status_screaming_snake() {
        assert_eq!(
            serde_json::to_value(HypothesisStatus::Proposed).unwrap(),
            "PROPOSED"
        );
        assert_eq!(
            serde_json::to_value(HypothesisStatus::Supported).unwrap(),
            "SUPPORTED"
        );
        assert_eq!(
            serde_json::to_value(HypothesisStatus::Refuted).unwrap(),
            "REFUTED"
        );
        assert_eq!(
            serde_json::to_value(HypothesisStatus::Rejected).unwrap(),
            "REJECTED"
        );
    }
}
