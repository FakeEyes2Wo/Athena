//! Sub-agent lifecycle: spawn/wait/events/send_message/cancel + concurrency.
#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use athena_agent::AgentControl;
use common::*;
use std::time::Duration;

#[tokio::test]
async fn spawn_completes_and_streams_events() {
    let provider = FakeProvider::new(vec![vec![text_delta("hi"), completed()]]);
    let agent = make_agent(provider, vec![]);
    let control = AgentControl::new(4);

    let handle = control.spawn(agent, "do it".into(), None).await.unwrap();
    let result = handle.wait().await;
    assert_eq!(result.status, "completed");
    assert!(result.result_ref.starts_with("result://"));

    // The text-delta event was forwarded to the handle's stream.
    let mut saw_delta = false;
    while let Some(ev) = handle.next_event().await {
        if ev.kind == "agent/text_delta" {
            saw_delta = true;
        }
    }
    assert!(saw_delta);
}

#[tokio::test]
async fn send_message_unknown_agent_errors() {
    let control = AgentControl::new(2);
    assert!(control.send_message("sub-999", "hello").await.is_err());
}

#[tokio::test]
async fn cancel_marks_running_agent_cancelled() {
    // First sampling calls a tool that sleeps far longer than the test.
    let provider = FakeProvider::new(vec![vec![tool_call("c1", "sleeper"), completed()]]);
    let agent = make_agent(provider, vec![SleeperTool::new("sleeper")]);
    let control = AgentControl::new(4);

    let handle = control.spawn(agent, "long".into(), None).await.unwrap();
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert_eq!(control.list_agents().await, vec![handle.agent_id.clone()]);
    handle.cancel();

    let result = handle.wait().await;
    assert_eq!(result.status, "cancelled");
}
