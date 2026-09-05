//! Agent tool-loop behaviour with a scripted provider and fake tools.
#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use athena_agent::{AgentError, ProviderEvent};
use athena_memory::{MessagePart, MessageRole};
use common::*;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::{Mutex, watch};

#[tokio::test]
async fn text_only_response_completes() {
    let provider = FakeProvider::new(vec![vec![text_delta("hello world"), completed()]]);
    let agent = make_agent(provider, vec![]);
    let (sink, kinds) = RecordingSink::new();
    let (ctx, memory) = make_ctx(sink, "hi");

    let outcome = agent.run(ctx).await.unwrap();
    assert!(outcome.result_ref.as_str().starts_with("result://"));

    // A text delta was emitted, and the final assistant text is in context.
    assert!(kinds.lock().await.iter().any(|k| k == "agent/text_delta"));
    let items = memory.lock().await.items();
    assert!(items.iter().any(|m| {
        m.role == MessageRole::Assistant
            && m.parts
                .iter()
                .any(|p| matches!(p, MessagePart::Text { content } if content == "hello world"))
    }));
}

#[tokio::test]
async fn single_tool_call_then_completes() {
    let provider = FakeProvider::new(vec![
        vec![tool_call("call_1", "echo"), completed()],
        vec![text_delta("done"), completed()],
    ]);
    let agent = make_agent(provider, vec![EchoTool::new("echo", true)]);
    let (sink, _kinds) = RecordingSink::new();
    let (ctx, memory) = make_ctx(sink, "please echo");

    agent.run(ctx).await.unwrap();

    let items = memory.lock().await.items();
    // Assistant tool-call message + tool-return message are recorded in order.
    assert!(items.iter().any(|m| {
        m.parts
            .iter()
            .any(|p| matches!(p, MessagePart::ToolCall { tool_name, .. } if tool_name == "echo"))
    }));
    assert!(items.iter().any(|m| m.role == MessageRole::Tool
        && m.parts.iter().any(
            |p| matches!(p, MessagePart::ToolReturn { tool_name, .. } if tool_name == "echo")
        )));
}

#[tokio::test]
async fn non_safe_tool_is_a_serial_barrier() {
    let log = Arc::new(Mutex::new(Vec::<(String, u128, u128)>::new()));
    let base = Instant::now();
    let tools = vec![
        IntervalTool::new("safe_a", true, log.clone(), base, 60),
        IntervalTool::new("safe_b", true, log.clone(), base, 60),
        IntervalTool::new("barrier", false, log.clone(), base, 60),
        IntervalTool::new("safe_c", true, log.clone(), base, 60),
    ];
    let provider = FakeProvider::new(vec![
        vec![
            tool_call("c1", "safe_a"),
            tool_call("c2", "safe_b"),
            tool_call("c3", "barrier"),
            tool_call("c4", "safe_c"),
            completed(),
        ],
        vec![text_delta("done"), completed()],
    ]);
    let agent = make_agent(provider, tools);
    let (sink, _kinds) = RecordingSink::new();
    let (ctx, _memory) = make_ctx(sink, "go");
    agent.run(ctx).await.unwrap();

    let intervals = log.lock().await.clone();
    let get = |name: &str| intervals.iter().find(|(n, ..)| n == name).cloned().unwrap();
    let (_, a_s, a_e) = get("safe_a");
    let (_, b_s, b_e) = get("safe_b");
    let (_, bar_s, bar_e) = get("barrier");

    // The two leading safe tools overlap (ran in parallel).
    assert!(a_s < b_e && b_s < a_e, "safe_a and safe_b should overlap");
    // The barrier overlaps nothing.
    for (name, s, e) in &intervals {
        if name == "barrier" {
            continue;
        }
        let disjoint = *e <= bar_s || *s >= bar_e;
        assert!(disjoint, "{name} overlapped the serial barrier");
    }
}

#[tokio::test]
async fn provider_error_is_reported_not_success() {
    let provider = FakeProvider::new(vec![vec![ProviderEvent::Error {
        message: "stream broke".into(),
    }]]);
    let agent = make_agent(provider, vec![]);
    let (sink, _kinds) = RecordingSink::new();
    let (ctx, _memory) = make_ctx(sink, "hi");

    let result = agent.run(ctx).await;
    assert!(result.is_err(), "provider error must surface as an error");
}

#[tokio::test]
async fn pre_cancelled_agent_returns_cancelled_error() {
    let provider = FakeProvider::new(vec![]);
    let agent = make_agent(provider, vec![]);
    let (sink, _kinds) = RecordingSink::new();
    let (mut ctx, _memory) = make_ctx(sink, "hi");
    let (_cancel_tx, cancel_rx) = watch::channel(true);
    ctx.cancel = cancel_rx;

    assert!(matches!(agent.run(ctx).await, Err(AgentError::Cancelled)));
}
