pub mod envelope;
pub mod error;
pub mod method;
pub mod operations;

pub use envelope::{
    ClientMessage, ClientNotification, EventNotification, RequestEnvelope, ResponseEnvelope,
    ServerControlMessage, ServerRequest, ServerRequestReply,
};
pub use error::{ErrorCode, RpcError, RpcException};
pub use operations::{
    InterruptedResult, SubscribedResult, ThreadForkParams, ThreadForkedResult,
    ThreadStartParams, ThreadStartedResult, ThreadSubscribeParams, ThreadUnsubscribeParams,
    TurnInterruptParams, TurnStartParams, TurnStartedResult,
};

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use serde_json::json;

    #[test]
    fn test_request_serialize_roundtrip() {
        let req = RequestEnvelope {
            request_id: 42,
            method: "turn/start".into(),
            params: Some(json!({"thread_id": "t1"})),
        };
        let json = serde_json::to_string(&req).unwrap();
        let back: RequestEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(back.request_id, 42);
        assert_eq!(back.method, "turn/start");
    }

    #[test]
    fn test_response_with_error() {
        let resp = ResponseEnvelope {
            request_id: 1,
            result: None,
            error: Some(RpcError::closed("server shutting down")),
        };
        let json = serde_json::to_string(&resp).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["error"]["code"], -32006);
    }

    #[test]
    fn test_event_notification_serialize() {
        let ev = EventNotification {
            subscription_id: "sub:abc123".into(),
            thread_id: "t1".into(),
            turn_id: Some("turn-1".into()),
            sequence: 5,
            kind: "turn_completed".into(),
            event_ref: "athena-event:xyz".into(),
            data: None,
        };
        let json = serde_json::to_string(&ev).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["sequence"], 5);
    }

    #[test]
    fn test_is_control_method() {
        assert!(method::is_control_method(method::INITIALIZE));
        assert!(method::is_control_method(method::SERVER_SHUTDOWN));
        assert!(!method::is_control_method(method::TURN_START));
    }

    #[test]
    fn test_unknown_field_rejected() {
        let json = r#"{"request_id":1,"method":"test","params":null,"bad_field":1}"#;
        let err = serde_json::from_str::<RequestEnvelope>(json).unwrap_err();
        assert!(err.to_string().contains("bad_field"));
    }

    #[test]
    fn test_rpc_error_code_values_match_python() {
        assert_eq!(ErrorCode::InvalidArgument.code(), -32602);
        assert_eq!(ErrorCode::Internal.code(), -32603);
        assert_eq!(ErrorCode::DuplicateRequestId.code(), -32004);
    }

    #[test]
    fn test_method_constants_exist() {
        // Verify all method constants from the Python fixture exist
        assert_eq!(method::INITIALIZE, "initialize");
        assert_eq!(method::INITIALIZED, "initialized");
        assert_eq!(method::THREAD_START, "thread/start");
        assert_eq!(method::THREAD_FORK, "thread/fork");
        assert_eq!(method::TURN_START, "turn/start");
        assert_eq!(method::TURN_INTERRUPT, "turn/interrupt");
        assert_eq!(method::THREAD_SUBSCRIBE, "thread/subscribe");
        assert_eq!(method::THREAD_UNSUBSCRIBE, "thread/unsubscribe");
        assert_eq!(method::SERVER_SHUTDOWN, "server/shutdown");
        assert_eq!(method::ITEM_APPROVAL_REQUEST, "item/approval/request");
        assert_eq!(method::ITEM_USER_INPUT_REQUEST, "item/userInput/request");
        assert_eq!(method::TOOL_CALL_REQUEST, "tool/call/request");
    }

    #[test]
    fn test_request_id_zero_is_valid() {
        let req = RequestEnvelope {
            request_id: 0,
            method: "test".into(),
            params: None,
        };
        let json = serde_json::to_string(&req).unwrap();
        let back: RequestEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(back.request_id, 0);
    }

    #[test]
    fn test_request_id_negative_is_rejected() {
        let json = r#"{"request_id":-1,"method":"test"}"#;
        let err = serde_json::from_str::<RequestEnvelope>(json);
        assert!(err.is_err());
    }

    #[test]
    fn test_thread_start_params_deserialize() {
        let json = json!({
            "session_id": "sess-1",
            "context_ref": "artifact://ctx/init"
        });
        let params: ThreadStartParams = serde_json::from_value(json).unwrap();
        assert_eq!(params.session_id, "sess-1");
        assert_eq!(params.context_ref.as_str(), "artifact://ctx/init");
    }

    #[test]
    fn test_turn_interrupt_params_default_reason() {
        let json = json!({
            "thread_id": "t1",
            "turn_id": "turn-1"
        });
        let params: TurnInterruptParams = serde_json::from_value(json).unwrap();
        assert_eq!(params.reason, "user_requested");
    }

    #[test]
    fn test_turn_interrupt_params_explicit_reason() {
        let json = json!({
            "thread_id": "t1",
            "turn_id": "turn-1",
            "reason": "timeout"
        });
        let params: TurnInterruptParams = serde_json::from_value(json).unwrap();
        assert_eq!(params.reason, "timeout");
    }

    #[test]
    fn test_response_envelope_both_null() {
        // The protocol allows both result and error to be null/absent
        let json = r#"{"request_id":42}"#;
        let resp: ResponseEnvelope = serde_json::from_str(json).unwrap();
        assert_eq!(resp.request_id, 42);
        assert!(resp.result.is_none());
        assert!(resp.error.is_none());
    }
}
