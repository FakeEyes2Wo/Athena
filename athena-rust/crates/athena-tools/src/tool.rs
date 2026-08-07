use async_trait::async_trait;
use serde_json::Value;

use crate::context::ToolContext;
use crate::spec::ToolSpec;

// ── Event constants ──

pub const TOOL_BEGIN: &str = "tool/begin";
pub const TOOL_END: &str = "tool/end";
pub const TOOL_ERROR: &str = "tool/error";

// ── EventSink trait ──

pub trait EventSink: Send + Sync {
    fn emit(&self, kind: &str, call_id: &str, data: Option<Value>);
}

// ── ToolError ──

#[derive(Debug, Clone, thiserror::Error)]
pub enum ToolError {
    #[error("Tool not found")]
    NotFound,

    #[error("Invalid input: {0}")]
    InvalidInput(String),

    #[error("Execution failed: {0}")]
    ExecutionFailed(String),

    #[error("Tool execution cancelled")]
    Cancelled,
}

// ── Tool trait ──

#[async_trait]
pub trait Tool: Send + Sync {
    fn spec(&self) -> &ToolSpec;

    /// Execute the tool's business logic.
    async fn execute(&self, input: Value, ctx: &ToolContext) -> Result<Value, ToolError>;
}
