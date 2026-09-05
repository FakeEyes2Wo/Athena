use serde::{Deserialize, Serialize};

/// Protocol error codes matching Python ErrorCode(IntEnum) values.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(i64)]
pub enum ErrorCode {
    InvalidArgument = -32602,
    NotFound = -32601,
    FailedPrecondition = -32000,
    NotInitialized = -32002,
    AlreadyInitialized = -32003,
    DuplicateRequestId = -32004,
    Overloaded = -32005,
    Closed = -32006,
    Internal = -32603,
}

/// Protocol-level error only carries stable information; never tracebacks.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RpcError {
    pub code: i64,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<serde_json::Value>,
}

impl RpcError {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code: code as i64,
            message: message.into(),
            data: None,
        }
    }
}
