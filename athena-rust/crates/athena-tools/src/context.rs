use tokio::sync::watch;

/// Per-invocation context passed to every tool execution.
pub struct ToolContext {
    pub tool_name: String,
    pub call_id: String,
    pub cancel: watch::Receiver<bool>,
}
