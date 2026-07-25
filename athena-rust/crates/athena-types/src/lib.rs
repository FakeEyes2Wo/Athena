use serde::{Deserialize, Serialize};

/// Stable artifact reference string.
pub type ArtifactRef = String;

/// Direction for metric optimization.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MetricDirection {
    Maximize,
    Minimize,
}

/// Specification of a single evaluation metric.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MetricSpec {
    pub name: String,
    pub direction: MetricDirection,
}

/// Metadata describing a machine-learning task.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskMetaData {
    pub task_type: String,
    pub data_type: String,
    #[serde(default)]
    pub target_vars: Vec<String>,
    pub primary_metric: MetricSpec,
    #[serde(default)]
    pub constraints: Vec<String>,
}

/// Descriptive card for a dataset artifact.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DataCard {
    pub dataset_ref: ArtifactRef,
    pub fingerprint: String,
    pub schema_ref: ArtifactRef,
    #[serde(default)]
    pub split_manifest_ref: Option<ArtifactRef>,
}

/// Possible states for a hypothesis.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum HypothesisStatus {
    #[default]
    Proposed,
    Supported,
    Refuted,
    Rejected,
}

/// A falsifiable hypothesis that can be tested experimentally.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Hypothesis {
    pub statement: String,
    pub intervention: String,
    pub expected_effect: String,
    #[serde(default)]
    pub status: HypothesisStatus,
    #[serde(default)]
    pub evidence_refs: Vec<ArtifactRef>,
    #[serde(default)]
    pub patience_grant: u32,
    #[serde(default)]
    pub patience_evidence_ref: Option<ArtifactRef>,
}

/// An experimental plan describing a code change and its acceptance criteria.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExperimentPlan {
    pub kind: String,
    pub change: String,
    pub rubrics: Vec<String>,
    pub run_config_ref: ArtifactRef,
    pub budget: serde_json::Value,
    pub acceptance_rule: String,
}

/// A conversation thread within a session.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AthenaThread {
    pub thread_id: String,
    pub session_id: String,
    pub status: String,
    pub context_ref: ArtifactRef,
}

/// A single turn (request/response cycle) within a thread.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AthenaTurn {
    pub turn_id: String,
    pub thread_id: String,
    pub request_ref: ArtifactRef,
    pub status: String,
    #[serde(default)]
    pub result_ref: Option<ArtifactRef>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_serialize_hypothesis() {
        let h = Hypothesis {
            statement: "X improves accuracy".into(),
            intervention: "Add layer".into(),
            expected_effect: "Accuracy +2%".into(),
            status: HypothesisStatus::Proposed,
            evidence_refs: vec![],
            patience_grant: 0,
            patience_evidence_ref: None,
        };
        let json = serde_json::to_string(&h).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["status"], "PROPOSED");
        assert_eq!(parsed["statement"], "X improves accuracy");
    }

    #[test]
    fn test_deserialize_unknown_field_is_denied() {
        let json = r#"{"thread_id":"t1","session_id":"s1","status":"idle","context_ref":"ctx://1","extra_field":true}"#;
        let err = serde_json::from_str::<AthenaThread>(json).unwrap_err();
        assert!(err.to_string().contains("extra_field"));
    }

    #[test]
    fn test_serialize_thread_roundtrip() {
        let t = AthenaThread {
            thread_id: "abc".into(),
            session_id: "sess-1".into(),
            status: "idle".into(),
            context_ref: "ctx://init".into(),
        };
        let json = serde_json::to_string(&t).unwrap();
        let back: AthenaThread = serde_json::from_str(&json).unwrap();
        assert_eq!(back.thread_id, "abc");
        assert_eq!(back.status, "idle");
    }
}
