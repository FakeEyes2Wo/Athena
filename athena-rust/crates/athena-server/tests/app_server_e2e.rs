//! End-to-end: initialize → thread/start → turn/start → subscribe → event →
//! shutdown, driven through the real transport, processor, and client.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use athena_runtime::{
    EventDraft, RunnerError, RuntimeThreadManager, TurnInput, TurnOutput, TurnRunner,
};
use athena_server::{AppServer, ServerEvent};
use athena_types::ArtifactRef;
use serde_json::json;
use std::sync::Arc;
use std::time::Duration;

/// Emits one event, then completes.
struct EchoRunner;

#[async_trait::async_trait]
impl TurnRunner for EchoRunner {
    async fn run(&self, input: TurnInput) -> Result<TurnOutput, RunnerError> {
        let _ = input
            .emit
            .emit(EventDraft::new("message", "athena-event:ack"))
            .await;
        Ok(TurnOutput {
            result_ref: ArtifactRef::new(format!("result://{}", input.turn.turn_id.as_str()))
                .map_err(|e| RunnerError(e.to_string()))?,
            next_context_ref: ArtifactRef::new(format!("ctx://{}", input.turn.turn_id.as_str()))
                .map_err(|e| RunnerError(e.to_string()))?,
        })
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn full_lifecycle_initialize_to_shutdown() {
    let manager = Arc::new(RuntimeThreadManager::new(Arc::new(EchoRunner)));
    let app = AppServer::create(manager).await.expect("server ready");

    let timeout = Duration::from_secs(3);

    // thread/start
    let thread = app
        .client
        .request(
            "thread/start",
            json!({"session_id": "sess-1", "context_ref": "artifact://ctx/init"}),
            timeout,
        )
        .await
        .unwrap();
    let thread_id = thread["thread_id"].as_str().unwrap().to_string();

    // turn/start
    let turn = app
        .client
        .request(
            "turn/start",
            json!({"thread_id": thread_id, "request_ref": "artifact://req"}),
            timeout,
        )
        .await
        .unwrap();
    assert!(turn["turn_id"].is_string());

    // thread/subscribe
    let sub = app
        .client
        .request(
            "thread/subscribe",
            json!({"thread_id": thread_id, "after_sequence": 0}),
            timeout,
        )
        .await
        .unwrap();
    assert!(sub["subscription_id"].as_str().unwrap().starts_with("sub:"));

    // Consume events until the turn completes.
    let mut saw_completed = false;
    for _ in 0..50 {
        match app.client.next_event(timeout).await {
            Some(ServerEvent::Event(ev)) => {
                if ev.kind == "turn_completed" {
                    saw_completed = true;
                    break;
                }
                if ev.kind == "turn_failed" {
                    break;
                }
            }
            Some(ServerEvent::Request(_)) => {}
            None => break,
        }
    }
    assert!(saw_completed, "expected a turn_completed event");

    app.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn business_request_before_ready_is_rejected() {
    // Drive the transport directly so we can send a business request without the
    // initialize handshake and observe the NOT_INITIALIZED rejection.
    use athena_protocol::{RequestEnvelope, ServerControlMessage};
    use athena_runtime::FairMux;
    use athena_server::{ExecutionAdapter, MessageProcessor, SubscriptionRegistry, transport};

    let manager = Arc::new(RuntimeThreadManager::new(Arc::new(EchoRunner)));
    let (mut client, server) = transport();
    let mux = Arc::new(FairMux::new(server.event_sender()));
    let subs = Arc::new(SubscriptionRegistry::new(mux.clone(), manager.clone()));
    let executor = Arc::new(ExecutionAdapter::new(manager.clone(), subs));
    let _processor = MessageProcessor::start(server, executor);

    client
        .send(athena_protocol::ClientMessage::Request(RequestEnvelope {
            request_id: 7,
            method: "thread/start".into(),
            params: Some(json!({"session_id": "s", "context_ref": "artifact://c"})),
        }))
        .await
        .unwrap();

    match client.recv_control().await {
        Some(ServerControlMessage::Response(resp)) => {
            assert_eq!(resp.request_id, 7);
            assert_eq!(resp.error.unwrap().code, -32002); // NOT_INITIALIZED
        }
        other => panic!("expected a response, got {other:?}"),
    }
}
