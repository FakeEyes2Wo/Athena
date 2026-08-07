// Allow unwrap/expect in test code only
#![allow(clippy::unwrap_used, clippy::expect_used)]

use athena_types::*;
use serde_json::Value;

const DOMAIN_JSON: &str = include_str!("../../../tests/fixtures/protocol/domain.json");

fn load_fixture() -> Value {
    serde_json::from_str(DOMAIN_JSON).expect("domain.json should be valid JSON")
}

#[test]
fn test_hypothesis_proposed_serialization_matches_python() {
    let fixture = load_fixture();
    let hyp: Hypothesis = serde_json::from_value(fixture["hypothesis_proposed"].clone())
        .expect("deserialize proposed");

    // Verify status is SCREAMING_SNAKE
    let serialized = serde_json::to_value(&hyp).unwrap();
    assert_eq!(serialized["status"], "PROPOSED");

    // Verify field names match Python (all snake_case in JSON)
    assert_eq!(
        hyp.statement,
        "Adding a batch-norm layer improves convergence"
    );
    assert_eq!(hyp.status, HypothesisStatus::Proposed);
    assert_eq!(hyp.patience_grant, 0);
    assert!(hyp.patience_evidence_ref.is_none());
}

#[test]
fn test_hypothesis_supported_deserialize() {
    let fixture = load_fixture();
    let hyp: Hypothesis = serde_json::from_value(fixture["hypothesis_supported"].clone())
        .expect("deserialize supported");

    assert_eq!(hyp.status, HypothesisStatus::Supported);
    assert_eq!(hyp.evidence_refs.len(), 1);
    assert_eq!(
        hyp.evidence_refs[0].as_str(),
        "artifact://exp/run-42/results"
    );
    assert_eq!(hyp.patience_grant, 0);
    assert!(hyp.patience_evidence_ref.is_none());
}

#[test]
fn test_hypothesis_refuted_with_patience() {
    let fixture = load_fixture();
    let hyp: Hypothesis =
        serde_json::from_value(fixture["hypothesis_refuted_with_patience"].clone())
            .expect("deserialize refuted with patience");

    assert_eq!(hyp.status, HypothesisStatus::Refuted);
    assert_eq!(hyp.patience_grant, 1);
    assert!(hyp.patience_evidence_ref.is_some());
    assert_eq!(
        hyp.patience_evidence_ref.as_ref().unwrap().as_str(),
        "artifact://exp/run-43/results"
    );
}

#[test]
fn test_hypothesis_positive_patience_without_evidence_is_rejected() {
    // This test verifies cross-field validation:
    // patience_grant > 0 requires patience_evidence_ref to be Some
    let json = serde_json::json!({
        "statement": "Invalid",
        "intervention": "None",
        "expected_effect": "None",
        "status": "PROPOSED",
        "evidence_refs": [],
        "patience_grant": 1,
        "patience_evidence_ref": null
    });

    let err = serde_json::from_value::<Hypothesis>(json);
    assert!(
        err.is_err(),
        "should reject positive patience without evidence"
    );
    let err_msg = err.unwrap_err().to_string();
    assert!(
        err_msg.contains("positive patience grant"),
        "error should mention the validation rule, got: {err_msg}"
    );
}

#[test]
fn test_athena_thread_deserialize_from_fixture() {
    let fixture = load_fixture();
    let thread: AthenaThread = serde_json::from_value(fixture["athena_thread"].clone())
        .expect("deserialize athena_thread");

    assert_eq!(thread.thread_id.as_str(), "t-001");
    assert_eq!(thread.session_id.as_str(), "sess-2026-07-25");
    assert_eq!(thread.status, ThreadStatus::Idle);
    assert_eq!(thread.context_ref.as_str(), "artifact://ctx/initial");
}

#[test]
fn test_athena_thread_roundtrip() {
    let fixture = load_fixture();
    let thread: AthenaThread =
        serde_json::from_value(fixture["athena_thread"].clone()).expect("deserialize");

    let serialized = serde_json::to_value(&thread).unwrap();
    assert_eq!(serialized["thread_id"], "t-001");
    assert_eq!(serialized["session_id"], "sess-2026-07-25");
    assert_eq!(serialized["status"], "idle");
    assert_eq!(serialized["context_ref"], "artifact://ctx/initial");
}

#[test]
fn test_athena_turn_deserialize_from_fixture() {
    let fixture = load_fixture();
    let turn: AthenaTurn =
        serde_json::from_value(fixture["athena_turn"].clone()).expect("deserialize athena_turn");

    assert_eq!(turn.turn_id.as_str(), "turn-001");
    assert_eq!(turn.thread_id.as_str(), "t-001");
    assert_eq!(turn.request_ref.as_str(), "artifact://req/hello");
    assert_eq!(turn.status, TurnStatus::Running);
    assert!(turn.result_ref.is_none());
}

#[test]
fn test_athena_turn_roundtrip() {
    let fixture = load_fixture();
    let turn: AthenaTurn =
        serde_json::from_value(fixture["athena_turn"].clone()).expect("deserialize");

    let serialized = serde_json::to_value(&turn).unwrap();
    assert_eq!(serialized["turn_id"], "turn-001");
    assert_eq!(serialized["thread_id"], "t-001");
    assert_eq!(serialized["status"], "running");
    assert_eq!(serialized["request_ref"], "artifact://req/hello");
    // result_ref is None, so it should be absent from serialized output
    assert!(serialized.get("result_ref").is_none());
}

#[test]
fn test_blank_artifact_ref_is_rejected() {
    let err = ArtifactRef::new("").unwrap_err();
    assert!(err.to_string().contains("empty or blank"));

    let err = ArtifactRef::new("   ").unwrap_err();
    assert!(err.to_string().contains("empty or blank"));
}

#[test]
fn test_blank_artifact_ref_rejected_on_deserialize() {
    let result = serde_json::from_value::<ArtifactRef>(Value::String("".into()));
    assert!(result.is_err());

    let result = serde_json::from_value::<ArtifactRef>(Value::String("   ".into()));
    assert!(result.is_err());
}

#[test]
fn test_data_card_deserialize() {
    let fixture = load_fixture();
    let card: DataCard =
        serde_json::from_value(fixture["data_card"].clone()).expect("deserialize data_card");

    assert_eq!(card.dataset_ref.as_str(), "artifact://datasets/cifar10");
    assert_eq!(card.fingerprint, "sha256:abc123def456");
    assert!(card.split_manifest_ref.is_some());
}

#[test]
fn test_metric_spec_maximize() {
    let fixture = load_fixture();
    let spec: MetricSpec =
        serde_json::from_value(fixture["metric_spec"].clone()).expect("deserialize metric_spec");

    assert_eq!(spec.name, "accuracy");
    assert_eq!(spec.direction, MetricDirection::Maximize);
}

#[test]
fn test_metric_spec_minimize() {
    let fixture = load_fixture();
    let spec: MetricSpec = serde_json::from_value(fixture["metric_spec_minimize"].clone())
        .expect("deserialize metric_spec_minimize");

    assert_eq!(spec.name, "loss");
    assert_eq!(spec.direction, MetricDirection::Minimize);
}

#[test]
fn test_hypothesis_status_rejected_unknown_fields() {
    let json = serde_json::json!({
        "statement": "test",
        "intervention": "test",
        "expected_effect": "test",
        "unknown_field": "should cause error"
    });
    let err = serde_json::from_value::<Hypothesis>(json);
    assert!(err.is_err());
}

#[test]
fn test_athena_thread_rejects_unknown_fields() {
    let json = serde_json::json!({
        "thread_id": "t1",
        "session_id": "s1",
        "status": "idle",
        "context_ref": "ctx://test",
        "bogus": true
    });
    let err = serde_json::from_value::<AthenaThread>(json);
    assert!(err.is_err());
}
