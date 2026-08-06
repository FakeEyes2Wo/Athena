use crate::ids::{ArtifactRef, ValidationError};
use serde::{Deserialize, Deserializer, Serialize};
use std::convert::TryFrom;

// ── MetricDirection ──

/// Direction for metric optimization.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MetricDirection {
    Maximize,
    Minimize,
}

// ── MetricSpec ──

/// Specification of a single evaluation metric.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MetricSpec {
    pub name: String,
    pub direction: MetricDirection,
}

// ── TaskMetaData ──

/// Metadata describing a machine-learning task.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
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

// ── DataCard ──

/// Descriptive card for a dataset artifact.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DataCard {
    pub dataset_ref: ArtifactRef,
    pub fingerprint: String,
    pub schema_ref: ArtifactRef,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub split_manifest_ref: Option<ArtifactRef>,
}

// ── HypothesisStatus ──

/// Possible states for a hypothesis, serialized as SCREAMING_SNAKE_CASE.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum HypothesisStatus {
    #[default]
    Proposed,
    Supported,
    Refuted,
    Rejected,
}

// ── Hypothesis ──

/// A falsifiable hypothesis that can be tested experimentally.
///
/// Cross-field invariant: when `patience_grant > 0`, `patience_evidence_ref`
/// MUST be `Some(...)`.  This is enforced during deserialization.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
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

/// Helper used only for deserialization to apply cross-field validation.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct HypothesisRaw {
    statement: String,
    intervention: String,
    expected_effect: String,
    #[serde(default)]
    status: HypothesisStatus,
    #[serde(default)]
    evidence_refs: Vec<ArtifactRef>,
    #[serde(default)]
    patience_grant: u32,
    #[serde(default)]
    patience_evidence_ref: Option<ArtifactRef>,
}

impl TryFrom<HypothesisRaw> for Hypothesis {
    type Error = ValidationError;

    fn try_from(raw: HypothesisRaw) -> Result<Self, Self::Error> {
        if raw.patience_grant > 0 && raw.patience_evidence_ref.is_none() {
            return Err(ValidationError(
                "positive patience grant requires an evidence reference".into(),
            ));
        }
        Ok(Hypothesis {
            statement: raw.statement,
            intervention: raw.intervention,
            expected_effect: raw.expected_effect,
            status: raw.status,
            evidence_refs: raw.evidence_refs,
            patience_grant: raw.patience_grant,
            patience_evidence_ref: raw.patience_evidence_ref,
        })
    }
}

impl<'de> Deserialize<'de> for Hypothesis {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = HypothesisRaw::deserialize(deserializer)?;
        Hypothesis::try_from(raw).map_err(serde::de::Error::custom)
    }
}

// ── ExperimentPlan ──

/// An experimental plan describing a code change and its acceptance criteria.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExperimentPlan {
    pub kind: String,
    pub change: String,
    pub rubrics: Vec<String>,
    pub run_config_ref: ArtifactRef,
    pub budget: serde_json::Value,
    pub acceptance_rule: String,
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn test_hypothesis_serialize_status_screaming_snake() {
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
    fn test_hypothesis_rejects_positive_patience_without_evidence() {
        let json = serde_json::json!({
            "statement": "Invalid",
            "intervention": "None",
            "expected_effect": "None",
            "patience_grant": 1,
            "patience_evidence_ref": null
        });
        let err = serde_json::from_value::<Hypothesis>(json).unwrap_err();
        assert!(err.to_string().contains("positive patience grant"));
    }

    #[test]
    fn test_hypothesis_accepts_positive_patience_with_evidence() {
        let json = serde_json::json!({
            "statement": "Valid",
            "intervention": "Change lr",
            "expected_effect": "Faster convergence",
            "patience_grant": 1,
            "patience_evidence_ref": "artifact://exp/run-43/results"
        });
        let h: Hypothesis = serde_json::from_value(json).unwrap();
        assert_eq!(h.patience_grant, 1);
        assert!(h.patience_evidence_ref.is_some());
        assert_eq!(
            h.patience_evidence_ref.as_ref().map(|r| r.as_str()),
            Some("artifact://exp/run-43/results")
        );
    }

    #[test]
    fn test_hypothesis_accepts_zero_patience_without_evidence() {
        let json = serde_json::json!({
            "statement": "Valid",
            "intervention": "None",
            "expected_effect": "None",
            "patience_grant": 0,
            "patience_evidence_ref": null
        });
        let h: Hypothesis = serde_json::from_value(json).unwrap();
        assert_eq!(h.patience_grant, 0);
        assert!(h.patience_evidence_ref.is_none());
    }

    #[test]
    fn test_metric_direction_serialize_snake_case() {
        let m = MetricSpec {
            name: "accuracy".into(),
            direction: MetricDirection::Maximize,
        };
        let json = serde_json::to_value(&m).unwrap();
        assert_eq!(json["direction"], "maximize");
    }

    #[test]
    fn test_metric_direction_deserialize_snake_case() {
        let json = serde_json::json!({"name": "loss", "direction": "minimize"});
        let m: MetricSpec = serde_json::from_value(json).unwrap();
        assert_eq!(m.direction, MetricDirection::Minimize);
    }

    #[test]
    fn test_metric_direction_rejects_unknown() {
        let json = serde_json::json!({"name": "acc", "direction": "unknown"});
        let err = serde_json::from_value::<MetricSpec>(json).unwrap_err();
        assert!(err.is_data());
    }
}
