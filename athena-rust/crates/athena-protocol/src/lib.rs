pub mod error;

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub use error::{ErrorCode, RpcError, RpcException};

// ── Method name constants ──

pub mod method {
    pub const INITIALIZE: &str = "initialize";
    pub const INITIALIZED: &str = "initialized";
    pub const THREAD_START: &str = "thread/start";
    pub const THREAD_FORK: &str = "thread/fork";
    pub const TURN_START: &str = "turn/start";
    pub const TURN_INTERRUPT: &str = "turn/interrupt";
    pub const THREAD_SUBSCRIBE: &str = "thread/subscribe";
    pub const THREAD_UNSUBSCRIBE: &str = "thread/unsubscribe";
    pub const SERVER_SHUTDOWN: &str = "server/shutdown";
    pub const ITEM_APPROVAL_REQUEST: &str = "item/approval/request";

    pub fn is_control_method(m: &str) -> bool {
        matches!(m, INITIALIZE | SERVER_SHUTDOWN)
    }
}

// ── Request / Response envelopes ──

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RequestEnvelope {
    pub request_id: u64,
    pub method: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResponseEnvelope {
    pub request_id: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<RpcError>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClientNotification {
    pub method: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerRequest {
    pub server_call_id: String,
    pub method: String,
    pub params: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerRequestReply {
    pub server_call_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<RpcError>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EventNotification {
    pub subscription_id: String,
    pub thread_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    pub sequence: u64,
    pub kind: String,
    pub event_ref: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

// ── Parameter DTOs ──

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThreadStartParams {
    pub session_id: String,
    pub context_ref: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TurnStartParams {
    pub thread_id: String,
    pub request_ref: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TurnInterruptParams {
    pub thread_id: String,
    pub turn_id: String,
    #[serde(default)]
    pub reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThreadForkParams {
    pub thread_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub after_turn_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThreadSubscribeParams {
    pub thread_id: String,
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
    pub thread_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TurnStartedResult {
    pub turn_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThreadForkedResult {
    pub thread_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubscribedResult {
    pub subscription_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InterruptedResult {
    pub turn_id: String,
    #[serde(default = "default_interrupted_status")]
    pub status: String,
}

fn default_interrupted_status() -> String {
    "interrupted".into()
}

// ── Client/Server message union ──

#[derive(Debug, Clone)]
pub enum ClientMessage {
    Request(RequestEnvelope),
    Notification(ClientNotification),
    ServerRequestReply(ServerRequestReply),
}

#[derive(Debug, Clone)]
pub enum ServerControlMessage {
    Response(ResponseEnvelope),
    ServerRequest(ServerRequest),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_request_serialize_roundtrip() {
        let req = RequestEnvelope {
            request_id: 42,
            method: "turn/start".into(),
            params: Some(serde_json::json!({"thread_id": "t1"})),
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
}
