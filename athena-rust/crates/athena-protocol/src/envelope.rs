use crate::error::RpcError;
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// A request sent from client to server.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RequestEnvelope {
    pub request_id: u64,
    pub method: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
}

/// A response sent from server to client.
///
/// Both `result` and `error` can be `None` simultaneously (the protocol
/// does not forbid this in v1).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResponseEnvelope {
    pub request_id: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<RpcError>,
}

/// A one-way notification from client to server (no response expected).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClientNotification {
    pub method: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
}

/// A request initiated by the server to the client (e.g. approval, tool call).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerRequest {
    pub server_call_id: String,
    pub method: String,
    pub params: Value,
}

/// Reply to a `ServerRequest`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerRequestReply {
    pub server_call_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<RpcError>,
}

/// An event notification pushed by the server to subscribers.
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

// ── Client/Server message unions ──

/// All message types the client can send.
#[derive(Debug, Clone)]
pub enum ClientMessage {
    Request(RequestEnvelope),
    Notification(ClientNotification),
    ServerRequestReply(ServerRequestReply),
}

/// All control-plane messages the server can send.
#[derive(Debug, Clone)]
pub enum ServerControlMessage {
    Response(ResponseEnvelope),
    ServerRequest(ServerRequest),
}
