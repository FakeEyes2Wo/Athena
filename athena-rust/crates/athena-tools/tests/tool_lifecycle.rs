#![allow(clippy::unwrap_used)]

use std::sync::{Arc, Mutex};

use serde_json::{Value, json};
use tokio::sync::watch;

use athena_tools::*;

// ── Mocks ──

enum MockBehavior {
    Success(Value),
    Error(String),
    Cancelled,
    LongResult(usize),
}

struct MockTool {
    spec: ToolSpec,
    behavior: MockBehavior,
}

#[async_trait::async_trait]
impl Tool for MockTool {
    fn spec(&self) -> &ToolSpec {
        &self.spec
    }

    async fn execute(&self, _input: Value, ctx: &ToolContext) -> Result<Value, ToolError> {
        if *ctx.cancel.borrow() {
            return Err(ToolError::Cancelled);
        }
        match &self.behavior {
            MockBehavior::Success(data) => Ok(data.clone()),
            MockBehavior::Error(msg) => Err(ToolError::ExecutionFailed(msg.clone())),
            MockBehavior::Cancelled => Err(ToolError::Cancelled),
            MockBehavior::LongResult(n) => Ok(Value::String("x".repeat(*n))),
        }
    }
}

type EventRecord = (String, String, Option<Value>);

#[derive(Clone)]
struct MockEventSink {
    events: Arc<Mutex<Vec<EventRecord>>>,
}

impl MockEventSink {
    fn new() -> Self {
        Self {
            events: Arc::new(Mutex::new(Vec::new())),
        }
    }

    #[allow(clippy::unwrap_used)]
    fn events(&self) -> Vec<EventRecord> {
        self.events.lock().unwrap().clone()
    }
}

impl EventSink for MockEventSink {
    fn emit(&self, kind: &str, call_id: &str, data: Option<Value>) {
        if let Ok(mut events) = self.events.lock() {
            events.push((kind.to_string(), call_id.to_string(), data));
        }
    }
}

fn make_context(call_id: &str, cancel: watch::Receiver<bool>) -> ToolContext {
    ToolContext {
        tool_name: "mock".into(),
        call_id: call_id.into(),
        cancel,
    }
}

// ── Helpers ──

fn spec(name: &str, max_result_chars: usize) -> ToolSpec {
    ToolSpec {
        name: name.into(),
        description: "test tool".into(),
        input_schema: json!({"type": "object"}),
        concurrency_safe: true,
        max_result_chars,
    }
}

// ── Tests ──

#[tokio::test]
async fn test_begin_end_events_fire_in_order() {
    let tool = MockTool {
        spec: spec("test_tool", 50_000),
        behavior: MockBehavior::Success(json!({"result": "ok"})),
    };
    let sink = MockEventSink::new();
    let executor = ToolExecutor::new(Arc::new(sink.clone()));
    let (_tx, rx) = watch::channel(false);
    let ctx = make_context("call_1", rx);

    let result = executor.invoke(&tool, json!({"key": "value"}), &ctx).await;

    // Should succeed
    let result = result.unwrap();
    assert!(result.success);
    assert_eq!(result.data, Some(json!({"result": "ok"})));

    // Exactly two events: begin → end
    let events = sink.events();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].0, TOOL_BEGIN);
    assert_eq!(events[1].0, TOOL_END);

    // Same call_id
    assert_eq!(events[0].1, "call_1");
    assert_eq!(events[1].1, "call_1");
}

#[tokio::test]
async fn test_error_event_sanitized() {
    let tool = MockTool {
        spec: spec("err_tool", 50_000),
        behavior: MockBehavior::Error("something broke".into()),
    };
    let sink = MockEventSink::new();
    let executor = ToolExecutor::new(Arc::new(sink.clone()));
    let (_tx, rx) = watch::channel(false);
    let ctx = make_context("call_2", rx);

    let result = executor.invoke(&tool, json!({"key": "value"}), &ctx).await;

    // Error should be wrapped in Ok(ToolResult) — not propagated
    let result = result.unwrap();
    assert!(!result.success);
    assert!(result.error.is_some());

    // Message should be sanitised — no traceback
    let msg = result.error.as_ref().unwrap();
    assert!(msg.contains("Execution failed"));
    assert!(msg.contains("something broke"));
    assert!(!msg.contains("\\n")); // no newline/stacktrace

    // Events: begin → error
    let events = sink.events();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].0, TOOL_BEGIN);
    assert_eq!(events[1].0, TOOL_ERROR);

    // TOOL_ERROR carries the sanitised message
    if let Some(Value::Object(ref obj)) = events[1].2 {
        assert_eq!(
            obj.get("error").and_then(|v| v.as_str()),
            Some("Execution failed: something broke")
        );
    } else {
        panic!("TOOL_ERROR event should carry data with error field");
    }
}

#[tokio::test]
async fn test_cancelled_propagates_as_err() {
    let tool = MockTool {
        spec: spec("cancel_tool", 50_000),
        behavior: MockBehavior::Cancelled, // always returns Cancelled
    };
    let sink = MockEventSink::new();
    let executor = ToolExecutor::new(Arc::new(sink.clone()));
    let (_tx, rx) = watch::channel(false);
    let ctx = make_context("call_3", rx);

    let result = executor.invoke(&tool, json!({"key": "value"}), &ctx).await;

    // Cancelled must be propagated as Err(ToolError::Cancelled), NOT wrapped
    match result {
        Err(ToolError::Cancelled) => { /* expected */ }
        other => panic!("Expected Err(ToolError::Cancelled), got {:?}", other),
    }

    // Events: begin → error
    let events = sink.events();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].0, TOOL_BEGIN);
    assert_eq!(events[1].0, TOOL_ERROR);
}

#[tokio::test]
async fn test_cancelled_by_signal_also_propagates() {
    // Tool that checks the cancel signal
    struct CheckCancelTool {
        spec: ToolSpec,
    }

    #[async_trait::async_trait]
    impl Tool for CheckCancelTool {
        fn spec(&self) -> &ToolSpec {
            &self.spec
        }

        async fn execute(&self, _input: Value, ctx: &ToolContext) -> Result<Value, ToolError> {
            if *ctx.cancel.borrow() {
                return Err(ToolError::Cancelled);
            }
            Ok(json!({"fine": "ok"}))
        }
    }

    let tool = CheckCancelTool {
        spec: spec("check_cancel", 50_000),
    };
    let sink = MockEventSink::new();
    let executor = ToolExecutor::new(Arc::new(sink.clone()));
    let (tx, rx) = watch::channel(false);
    let ctx = make_context("call_cancel", rx);

    // Send cancel signal *before* execution
    let _ = tx.send(true);

    let result = executor.invoke(&tool, json!({"key": "value"}), &ctx).await;

    match result {
        Err(ToolError::Cancelled) => { /* expected */ }
        other => panic!("Expected Err(ToolError::Cancelled), got {:?}", other),
    }

    let events = sink.events();
    assert_eq!(events.len(), 2);
    assert_eq!(events[1].0, TOOL_ERROR);
}

#[tokio::test]
async fn test_long_result_truncated() {
    // max_result_chars = 10 means the JSON serialisation of the result
    // will be truncated.  The tool returns a string "xxxx…" (100 chars)
    // whose JSON representation is "\"xxxx…\"" (102 chars).
    let tool = MockTool {
        spec: spec("long_tool", 10),
        behavior: MockBehavior::LongResult(100),
    };
    let sink = MockEventSink::new();
    let executor = ToolExecutor::new(Arc::new(sink.clone()));
    let (_tx, rx) = watch::channel(false);
    let ctx = make_context("call_4", rx);

    let result = executor.invoke(&tool, json!({}), &ctx).await;

    let result = result.unwrap();
    assert!(result.truncated, "long result should be flagged truncated");

    // The data should be smaller than the original 100-char string
    if let Some(Value::String(ref s)) = result.data {
        assert!(
            s.len() < 100,
            "truncated data should be shorter than original"
        );
    } else {
        panic!("expected string data from long result");
    }
}

#[tokio::test]
async fn test_empty_ok_result() {
    let tool = MockTool {
        spec: spec("empty_tool", 50_000),
        behavior: MockBehavior::Success(Value::Null),
    };
    let sink = MockEventSink::new();
    let executor = ToolExecutor::new(Arc::new(sink.clone()));
    let (_tx, rx) = watch::channel(false);
    let ctx = make_context("call_5", rx);

    let result = executor.invoke(&tool, json!({}), &ctx).await;

    let result = result.unwrap();
    assert!(result.success);
    assert!(!result.truncated);
}
