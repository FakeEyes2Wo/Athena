#![allow(clippy::unwrap_used, clippy::expect_used)]

use athena_types::{ArtifactRef, AthenaThread, AthenaTurn, ThreadStatus, TurnStatus};
use serde_json::Value;

const DOMAIN_JSON: &str = include_str!("../../../tests/fixtures/protocol/domain.json");

fn load_fixture() -> Value {
    serde_json::from_str(DOMAIN_JSON).expect("domain.json should be valid JSON")
}

#[test]
fn test_athena_thread_deserialize_from_fixture() {
    let thread: AthenaThread = serde_json::from_value(load_fixture()["athena_thread"].clone())
        .expect("deserialize athena_thread");

    assert_eq!(thread.thread_id.as_str(), "t-001");
    assert_eq!(thread.session_id.as_str(), "sess-2026-07-25");
    assert_eq!(thread.status, ThreadStatus::Idle);
    assert_eq!(thread.context_ref.as_str(), "artifact://ctx/initial");
}

#[test]
fn test_athena_thread_roundtrip() {
    let thread: AthenaThread = serde_json::from_value(load_fixture()["athena_thread"].clone())
        .expect("deserialize athena_thread");
    let serialized = serde_json::to_value(&thread).unwrap();

    assert_eq!(serialized["thread_id"], "t-001");
    assert_eq!(serialized["session_id"], "sess-2026-07-25");
    assert_eq!(serialized["status"], "idle");
    assert_eq!(serialized["context_ref"], "artifact://ctx/initial");
}

#[test]
fn test_athena_turn_deserialize_from_fixture() {
    let turn: AthenaTurn = serde_json::from_value(load_fixture()["athena_turn"].clone())
        .expect("deserialize athena_turn");

    assert_eq!(turn.turn_id.as_str(), "turn-001");
    assert_eq!(turn.thread_id.as_str(), "t-001");
    assert_eq!(turn.request_ref.as_str(), "artifact://req/hello");
    assert_eq!(turn.status, TurnStatus::Running);
    assert!(turn.result_ref.is_none());
}

#[test]
fn test_athena_turn_roundtrip() {
    let turn: AthenaTurn = serde_json::from_value(load_fixture()["athena_turn"].clone())
        .expect("deserialize athena_turn");
    let serialized = serde_json::to_value(&turn).unwrap();

    assert_eq!(serialized["turn_id"], "turn-001");
    assert_eq!(serialized["thread_id"], "t-001");
    assert_eq!(serialized["status"], "running");
    assert_eq!(serialized["request_ref"], "artifact://req/hello");
    assert!(serialized.get("result_ref").is_none());
}

#[test]
fn test_blank_artifact_ref_is_rejected() {
    for value in ["", "   "] {
        assert!(ArtifactRef::new(value).is_err());
        assert!(serde_json::from_value::<ArtifactRef>(Value::String(value.into())).is_err());
    }
}

#[test]
fn test_athena_thread_rejects_unknown_fields() {
    let mut thread = load_fixture()["athena_thread"].clone();
    thread["bogus"] = Value::Bool(true);
    assert!(serde_json::from_value::<AthenaThread>(thread).is_err());
}
