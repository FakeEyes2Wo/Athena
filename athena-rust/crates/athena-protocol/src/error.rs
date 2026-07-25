use serde::{Deserialize, Serialize};
use std::error::Error;
use std::fmt;

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

impl ErrorCode {
    pub fn code(self) -> i64 {
        self as i64
    }

    pub fn from_exception_name(name: &str) -> Self {
        match name {
            "ValueError" => Self::InvalidArgument,
            "KeyError" => Self::NotFound,
            "RuntimeError" => Self::FailedPrecondition,
            _ => Self::Internal,
        }
    }
}

/// Protocol-level error only carries stable information — no tracebacks.
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
        Self { code: code.code(), message: message.into(), data: None }
    }

    pub fn invalid_argument(msg: impl Into<String>) -> Self {
        Self::new(ErrorCode::InvalidArgument, msg)
    }

    pub fn not_found(msg: impl Into<String>) -> Self {
        Self::new(ErrorCode::NotFound, msg)
    }

    pub fn internal(msg: impl Into<String>) -> Self {
        Self::new(ErrorCode::Internal, msg)
    }

    pub fn closed(msg: impl Into<String>) -> Self {
        Self::new(ErrorCode::Closed, msg)
    }

    pub fn overloaded(msg: impl Into<String>) -> Self {
        Self::new(ErrorCode::Overloaded, msg)
    }

    pub fn not_initialized(msg: impl Into<String>) -> Self {
        Self::new(ErrorCode::NotInitialized, msg)
    }
}

/// Application-layer exception carrying an RpcError.
#[derive(Debug, Clone)]
pub struct RpcException {
    pub code: i64,
    pub message: String,
    pub data: Option<serde_json::Value>,
}

impl fmt::Display for RpcException {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "RPC error {}: {}", self.code, self.message)
    }
}

impl Error for RpcException {}

impl From<RpcError> for RpcException {
    fn from(e: RpcError) -> Self {
        Self { code: e.code, message: e.message, data: e.data }
    }
}
