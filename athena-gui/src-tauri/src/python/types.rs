use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Serialize, Deserialize, Clone, PartialEq)]
pub struct RpcError {
    pub code: i64,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

impl RpcError {
    pub fn transport(message: impl Into<String>) -> Self {
        Self {
            code: -32000,
            message: message.into(),
            data: None,
        }
    }
}

impl std::fmt::Display for RpcError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for RpcError {}

pub type RpcResult<T> = Result<T, RpcError>;

pub fn decode_response(response: Value) -> RpcResult<Value> {
    if let Some(error) = response.get("error").filter(|value| !value.is_null()) {
        let error = serde_json::from_value(error.clone())
            .map_err(|error| RpcError::transport(format!("invalid RPC error: {error}")))?;
        return Err(error);
    }
    Ok(response.get("result").cloned().unwrap_or(Value::Null))
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct EventNotification {
    #[serde(default)]
    pub subscription_id: Option<String>,
    pub kind: String,
    pub data: Value,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn deserializes_event_without_subscription_id() {
        // Python 侧 emit_event 只发 {"kind","data"}，没有 subscription_id。
        let val = json!({"kind": "output", "data": {"text": "hello", "source": "agent"}});
        let evt: EventNotification = serde_json::from_value(val).expect("deserialize event");
        assert_eq!(evt.kind, "output");
        assert!(evt.subscription_id.is_none());
        assert_eq!(evt.data["text"], "hello");
    }

    #[test]
    fn success_with_null_error_is_not_rejected() {
        let result = decode_response(json!({
            "request_id": 1,
            "result": {"ok": true},
            "error": null
        }))
        .expect("successful response");

        assert_eq!(result, json!({"ok": true}));
    }

    #[test]
    fn structured_domain_error_is_preserved() {
        let error = decode_response(json!({
            "request_id": 2,
            "result": null,
            "error": {
                "code": -32602,
                "message": "stale revision",
                "data": {"code": "stale_revision", "current_revision": 7}
            }
        }))
        .expect_err("domain error");

        assert_eq!(error.code, -32602);
        assert_eq!(error.data.unwrap()["code"], "stale_revision");
    }
}
