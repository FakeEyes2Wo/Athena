use std::sync::Arc;

use serde_json::Value;

use crate::context::ToolContext;
use crate::spec::ToolResult;
use crate::tool::{EventSink, TOOL_BEGIN, TOOL_END, TOOL_ERROR, Tool, ToolError};

/// Decorates a [`Tool`] with lifecycle events (begin/end/error) emitted to a
/// [`EventSink`], result truncation, and error sanitisation.
pub struct ToolExecutor {
    sink: Arc<dyn EventSink>,
}

impl ToolExecutor {
    pub fn new(sink: Arc<dyn EventSink>) -> Self {
        Self { sink }
    }

    /// Invoke `tool` with the given `input` and `ctx`.
    ///
    /// Lifecycle event order:
    ///
    /// 1. Emit `TOOL_BEGIN` with the input value.
    /// 2. Run `tool.execute`.
    /// 3. On success, emit `TOOL_END` and return `Ok(ToolResult)`.
    /// 4. On cancellation, emit `TOOL_ERROR` and return `Err(ToolError::Cancelled)`.
    /// 5. On other errors, emit `TOOL_ERROR` and return a sanitized failed result.
    ///
    /// The result is truncated to `tool.spec().max_result_chars` characters of
    /// its JSON representation.
    pub async fn invoke(
        &self,
        tool: &dyn Tool,
        input: Value,
        ctx: &ToolContext,
    ) -> Result<ToolResult, ToolError> {
        self.sink
            .emit(TOOL_BEGIN, &ctx.call_id, Some(input.clone()));

        match tool.execute(input, ctx).await {
            Ok(data) => {
                let max_chars = tool.spec().max_result_chars;
                let (truncated, data) = truncate_value(&data, max_chars);

                let result = ToolResult {
                    data: Some(data.clone()),
                    success: true,
                    error: None,
                    truncated,
                    artifacts: vec![],
                };

                self.sink.emit(TOOL_END, &ctx.call_id, Some(data));
                Ok(result)
            }

            Err(ToolError::Cancelled) => {
                self.sink.emit(TOOL_ERROR, &ctx.call_id, None);
                Err(ToolError::Cancelled)
            }

            Err(e) => {
                let msg = sanitize_message(&e);
                self.sink.emit(
                    TOOL_ERROR,
                    &ctx.call_id,
                    Some(serde_json::json!({"error": msg})),
                );
                Ok(ToolResult::err(msg))
            }
        }
    }
}

// ── helpers ──

fn sanitize_message(error: &ToolError) -> String {
    match error {
        ToolError::InvalidInput(msg) => format!("Invalid input: {}", msg),
        ToolError::ExecutionFailed(msg) => format!("Execution failed: {}", msg),
        ToolError::NotFound => "Tool not found".to_string(),
        ToolError::Cancelled => "Tool execution cancelled".to_string(),
    }
}

/// Truncate a JSON value so its string representation is at most `max_chars`
/// bytes/characters.  Returns `(was_truncated, truncated_value)`.
///
/// If the truncation boundary falls inside a JSON token the fallback is a
/// plain string containing the truncated serialisation.
fn truncate_value(value: &Value, max_chars: usize) -> (bool, Value) {
    let json_str = match serde_json::to_string(value) {
        Ok(s) => s,
        Err(_) => return (false, value.clone()),
    };

    if json_str.len() <= max_chars {
        return (false, value.clone());
    }

    let truncated_str = &json_str[..max_chars];

    // Try to re-parse as valid JSON. If the boundary cut mid-token this
    // will fail and we fall back to a plain-text string.
    if let Ok(truncated) = serde_json::from_str::<Value>(truncated_str) {
        (true, truncated)
    } else {
        (true, Value::String(format!("{}...", truncated_str)))
    }
}
