// Allow unwrap/expect in test code only
#![allow(clippy::unwrap_used, clippy::expect_used)]

use athena_protocol::*;
use serde_json::{Value, json};

const PROTOCOL_JSON: &str = include_str!("../../../tests/fixtures/protocol/protocol.json");

fn load_fixture() -> Value {
    serde_json::from_str(PROTOCOL_JSON).expect("protocol.json should be valid JSON")
}

// ── Error code tests ──

#[test]
fn test_all_error_code_values_match_python() {
    let fixture = load_fixture();
    let codes = fixture["error_codes"]
        .as_object()
        .expect("error_codes should be an object");

    assert_eq!(
        ErrorCode::InvalidArgument.code(),
        codes["INVALID_ARGUMENT"].as_i64().unwrap()
    );
    assert_eq!(
        ErrorCode::NotFound.code(),
        codes["NOT_FOUND"].as_i64().unwrap()
    );
    assert_eq!(
        ErrorCode::FailedPrecondition.code(),
        codes["FAILED_PRECONDITION"].as_i64().unwrap()
    );
    assert_eq!(
        ErrorCode::NotInitialized.code(),
        codes["NOT_INITIALIZED"].as_i64().unwrap()
    );
    assert_eq!(
        ErrorCode::AlreadyInitialized.code(),
        codes["ALREADY_INITIALIZED"].as_i64().unwrap()
    );
    assert_eq!(
        ErrorCode::DuplicateRequestId.code(),
        codes["DUPLICATE_REQUEST_ID"].as_i64().unwrap()
    );
    assert_eq!(
        ErrorCode::Overloaded.code(),
        codes["OVERLOADED"].as_i64().unwrap()
    );
    assert_eq!(ErrorCode::Closed.code(), codes["CLOSED"].as_i64().unwrap());
    assert_eq!(
        ErrorCode::Internal.code(),
        codes["INTERNAL"].as_i64().unwrap()
    );
}

#[test]
fn test_all_error_code_variants_covered() {
    // Verify we match the exact number of Python error codes
    let fixture = load_fixture();
    let codes = fixture["error_codes"].as_object().unwrap();
    assert_eq!(codes.len(), 9, "Python has exactly 9 error codes");
}

// ── Method constants tests ──

#[test]
fn test_all_method_constants_match_python() {
    let fixture = load_fixture();
    let methods = fixture["method_values"]
        .as_object()
        .expect("method_values should be an object");

    assert_eq!(method::INITIALIZE, methods["INITIALIZE"]);
    assert_eq!(method::INITIALIZED, methods["INITIALIZED"]);
    assert_eq!(method::THREAD_START, methods["THREAD_START"]);
    assert_eq!(method::THREAD_FORK, methods["THREAD_FORK"]);
    assert_eq!(method::TURN_START, methods["TURN_START"]);
    assert_eq!(method::TURN_INTERRUPT, methods["TURN_INTERRUPT"]);
    assert_eq!(method::THREAD_SUBSCRIBE, methods["THREAD_SUBSCRIBE"]);
    assert_eq!(method::THREAD_UNSUBSCRIBE, methods["THREAD_UNSUBSCRIBE"]);
    assert_eq!(method::SERVER_SHUTDOWN, methods["SERVER_SHUTDOWN"]);
    assert_eq!(
        method::ITEM_APPROVAL_REQUEST,
        methods["ITEM_APPROVAL_REQUEST"]
    );
    assert_eq!(
        method::ITEM_USER_INPUT_REQUEST,
        methods["ITEM_USER_INPUT_REQUEST"]
    );
    assert_eq!(method::TOOL_CALL_REQUEST, methods["TOOL_CALL_REQUEST"]);
}

#[test]
fn test_method_count_matches_python() {
    let fixture = load_fixture();
    let python_methods = fixture["methods"].as_array().unwrap();
    assert_eq!(python_methods.len(), 12, "Python has 12 method constants");
}

// ── RequestEnvelope fixtures ──

#[test]
fn test_request_envelope_matches_fixture() {
    let fixture = load_fixture();
    let req: RequestEnvelope = serde_json::from_value(fixture["request_envelope"].clone())
        .expect("deserialize request_envelope");

    assert_eq!(req.request_id, 42);
    assert_eq!(req.method, method::TURN_START);
    assert!(req.params.is_some());

    let params = req.params.unwrap();
    assert_eq!(params["thread_id"], "t1");
    assert_eq!(params["request_ref"], "artifact://test");
}

#[test]
fn test_request_envelope_roundtrip() {
    let fixture = load_fixture();
    let req: RequestEnvelope =
        serde_json::from_value(fixture["request_envelope"].clone()).expect("deserialize");

    let serialized = serde_json::to_value(&req).unwrap();
    assert_eq!(serialized["request_id"], 42);
    assert_eq!(serialized["method"], "turn/start");
}

// ── ResponseEnvelope fixtures ──

#[test]
fn test_response_envelope_error_matches_fixture() {
    let fixture = load_fixture();
    let resp: ResponseEnvelope = serde_json::from_value(fixture["response_envelope_error"].clone())
        .expect("deserialize response_envelope_error");

    assert_eq!(resp.request_id, 42);
    assert!(resp.result.is_none());
    let error = resp.error.expect("should have error");
    assert_eq!(error.code, -32006);
    assert_eq!(error.message, "server shutting down");
}

#[test]
fn test_response_envelope_success_matches_fixture() {
    let fixture = load_fixture();
    let resp: ResponseEnvelope =
        serde_json::from_value(fixture["response_envelope_success"].clone())
            .expect("deserialize response_envelope_success");

    assert_eq!(resp.request_id, 42);
    assert!(resp.error.is_none());
    let result = resp.result.expect("should have result");
    assert_eq!(result["turn_id"], "turn-1");
}

// ── EventNotification fixture ──

#[test]
fn test_event_notification_matches_fixture() {
    let fixture = load_fixture();
    let ev: EventNotification = serde_json::from_value(fixture["event_notification"].clone())
        .expect("deserialize event_notification");

    assert_eq!(ev.subscription_id, "sub:abc123");
    assert_eq!(ev.thread_id, "t1");
    assert_eq!(ev.turn_id.as_deref(), Some("turn-1"));
    assert_eq!(ev.sequence, 5);
    assert_eq!(ev.kind, "turn_completed");
    assert_eq!(ev.event_ref, "athena-event:xyz");
    assert!(ev.data.is_none());
}

#[test]
fn test_event_notification_roundtrip() {
    let fixture = load_fixture();
    let ev: EventNotification =
        serde_json::from_value(fixture["event_notification"].clone()).expect("deserialize");

    let serialized = serde_json::to_value(&ev).unwrap();
    assert_eq!(serialized["subscription_id"], "sub:abc123");
    assert_eq!(serialized["thread_id"], "t1");
    assert_eq!(serialized["turn_id"], "turn-1");
    assert_eq!(serialized["sequence"], 5);
    assert_eq!(serialized["kind"], "turn_completed");
    assert_eq!(serialized["event_ref"], "athena-event:xyz");
}

// ── ServerRequest fixture ──

#[test]
fn test_server_request_matches_fixture() {
    let fixture = load_fixture();
    let sr: ServerRequest = serde_json::from_value(fixture["server_request"].clone())
        .expect("deserialize server_request");

    assert_eq!(sr.server_call_id, "call-1");
    assert_eq!(sr.method, method::ITEM_APPROVAL_REQUEST);
    assert_eq!(sr.params["thread_id"], "t1");
    assert_eq!(sr.params["turn_id"], "turn-1");
}

// ── Operation params fixtures ──

#[test]
fn test_thread_start_params_matches_fixture() {
    let fixture = load_fixture();
    let params: operations::ThreadStartParams =
        serde_json::from_value(fixture["thread_start_params"].clone()).expect("deserialize");

    assert_eq!(params.session_id, "sess-1");
    assert_eq!(params.context_ref.as_str(), "artifact://ctx/init");
}

#[test]
fn test_turn_start_params_matches_fixture() {
    let fixture = load_fixture();
    let params: operations::TurnStartParams =
        serde_json::from_value(fixture["turn_start_params"].clone()).expect("deserialize");

    assert_eq!(params.thread_id.as_str(), "t1");
    assert_eq!(params.request_ref.as_str(), "artifact://req/test");
}

#[test]
fn test_turn_interrupt_params_matches_fixture() {
    let fixture = load_fixture();
    let params: operations::TurnInterruptParams =
        serde_json::from_value(fixture["turn_interrupt_params"].clone()).expect("deserialize");

    assert_eq!(params.thread_id.as_str(), "t1");
    assert_eq!(params.turn_id.as_str(), "turn-1");
    assert_eq!(params.reason, "user_requested");
}

#[test]
fn test_thread_fork_params_matches_fixture() {
    let fixture = load_fixture();
    let params: operations::ThreadForkParams =
        serde_json::from_value(fixture["thread_fork_params"].clone()).expect("deserialize");

    assert_eq!(params.thread_id.as_str(), "t1");
    assert!(params.after_turn_id.is_some());
    assert_eq!(params.after_turn_id.unwrap().as_str(), "turn-3");
}

#[test]
fn test_thread_subscribe_params_matches_fixture() {
    let fixture = load_fixture();
    let params: operations::ThreadSubscribeParams =
        serde_json::from_value(fixture["thread_subscribe_params"].clone()).expect("deserialize");

    assert_eq!(params.thread_id.as_str(), "t1");
    assert_eq!(params.after_sequence, 0);
}

#[test]
fn test_thread_unsubscribe_params_matches_fixture() {
    let fixture = load_fixture();
    let params: operations::ThreadUnsubscribeParams =
        serde_json::from_value(fixture["thread_unsubscribe_params"].clone()).expect("deserialize");

    assert_eq!(params.subscription_id, "sub:abc123");
}

// ── ClientNotification fixture ──

#[test]
fn test_client_notification_matches_fixture() {
    let fixture = load_fixture();
    let notif: ClientNotification = serde_json::from_value(fixture["client_notification"].clone())
        .expect("deserialize client_notification");

    assert_eq!(notif.method, method::INITIALIZED);
    assert!(notif.params.is_some());
    assert_eq!(notif.params.unwrap()["protocol_version"], 1);
}

// ── Unknown field rejection tests ──

#[test]
fn test_request_envelope_rejects_unknown_fields() {
    let json = json!({
        "request_id": 1,
        "method": "test",
        "extra_field": true
    });
    let err = serde_json::from_value::<RequestEnvelope>(json);
    assert!(err.is_err());
}

#[test]
fn test_response_envelope_rejects_unknown_fields() {
    let json = json!({
        "request_id": 1,
        "result": null,
        "bogus": "field"
    });
    let err = serde_json::from_value::<ResponseEnvelope>(json);
    assert!(err.is_err());
}

#[test]
fn test_event_notification_rejects_unknown_fields() {
    let json = json!({
        "subscription_id": "s1",
        "thread_id": "t1",
        "sequence": 0,
        "kind": "test",
        "event_ref": "ref",
        "unknown": "field"
    });
    let err = serde_json::from_value::<EventNotification>(json);
    assert!(err.is_err());
}

// ── request_id boundary tests ──

#[test]
fn test_request_id_zero_is_valid() {
    let json = json!({
        "request_id": 0,
        "method": "test"
    });
    let req: RequestEnvelope = serde_json::from_value(json).expect("request_id 0 should be valid");
    assert_eq!(req.request_id, 0);
}

#[test]
fn test_request_id_negative_is_invalid() {
    let json = json!({
        "request_id": -1,
        "method": "test"
    });
    let err = serde_json::from_value::<RequestEnvelope>(json);
    assert!(err.is_err(), "negative request_id should be rejected");
}

#[test]
fn test_request_id_large_is_valid() {
    let json = json!({
        "request_id": 18446744073709551615u64,
        "method": "test"
    });
    let req: RequestEnvelope = serde_json::from_value(json).expect("max u64 should be valid");
    assert_eq!(req.request_id, 18446744073709551615u64);
}

// ── RpcError factory methods ──

#[test]
fn test_rpc_error_invalid_argument() {
    let err = RpcError::invalid_argument("bad input");
    assert_eq!(err.code, -32602);
    assert_eq!(err.message, "bad input");
}

#[test]
fn test_rpc_error_not_found() {
    let err = RpcError::not_found("missing");
    assert_eq!(err.code, -32601);
}

#[test]
fn test_rpc_error_internal() {
    let err = RpcError::internal("oops");
    assert_eq!(err.code, -32603);
}

#[test]
fn test_rpc_error_closed() {
    let err = RpcError::closed("shutdown");
    assert_eq!(err.code, -32006);
}

#[test]
fn test_rpc_error_overloaded() {
    let err = RpcError::overloaded("busy");
    assert_eq!(err.code, -32005);
}

#[test]
fn test_rpc_error_not_initialized() {
    let err = RpcError::not_initialized("not ready");
    assert_eq!(err.code, -32002);
}
