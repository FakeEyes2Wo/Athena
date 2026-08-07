//! Lifecycle tests for the per-thread actor via `RuntimeThreadManager`.
#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use athena_runtime::{RuntimeError, RuntimeThreadManager, ThreadState};
use common::*;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Mutex;

async fn wait_for_terminal(handle: &athena_runtime::ThreadHandle) -> String {
    for _ in 0..500 {
        if let Some(ev) = handle
            .journal()
            .snapshot()
            .iter()
            .find(|e| is_terminal(&e.kind))
        {
            return ev.kind.clone();
        }
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
    panic!("no terminal event appeared");
}

#[tokio::test]
async fn turn_completes_and_records_snapshot() {
    let mgr = RuntimeThreadManager::new(Arc::new(CompletingRunner));
    let thread = mgr.start("sess-1", "ctx://init").await.unwrap();
    let turn = mgr
        .submit(thread.thread_id.as_str(), "req://1")
        .await
        .unwrap();
    let handle = mgr.get(thread.thread_id.as_str()).await.unwrap();

    assert_eq!(wait_for_terminal(&handle).await, "turn_completed");
    // Fork snapshot for the completed turn is the committed next context.
    let snap = handle
        .fork_snapshot(Some(turn.turn_id.as_str().to_string()))
        .await
        .unwrap();
    assert_eq!(snap.as_str(), format!("ctx://{}", turn.turn_id.as_str()));
    // Thread returns to idle.
    assert_eq!(handle.state(), ThreadState::Idle);
    mgr.close("done").await;
}

#[tokio::test]
async fn concurrent_start_is_rejected() {
    let rejected = Arc::new(Mutex::new(None));
    let mgr = RuntimeThreadManager::new(Arc::new(BlockingRunner {
        post_cancel_emit_rejected: rejected,
    }));
    let thread = mgr.start("sess-1", "ctx://init").await.unwrap();
    let handle = mgr.get(thread.thread_id.as_str()).await.unwrap();

    let _first = handle
        .start_turn(
            "turn-a".into(),
            athena_types::ArtifactRef::new("req://1").unwrap(),
        )
        .await
        .unwrap();
    let second = handle
        .start_turn(
            "turn-b".into(),
            athena_types::ArtifactRef::new("req://2").unwrap(),
        )
        .await;
    assert_eq!(second, Err(RuntimeError::AlreadyRunning));
    mgr.close("done").await;
}

#[tokio::test]
async fn failed_turn_emits_single_terminal() {
    let mgr = RuntimeThreadManager::new(Arc::new(FailingRunner));
    let thread = mgr.start("sess-1", "ctx://init").await.unwrap();
    mgr.submit(thread.thread_id.as_str(), "req://1")
        .await
        .unwrap();
    let handle = mgr.get(thread.thread_id.as_str()).await.unwrap();

    assert_eq!(wait_for_terminal(&handle).await, "turn_failed");
    let terminals = handle
        .journal()
        .snapshot()
        .iter()
        .filter(|e| is_terminal(&e.kind))
        .count();
    assert_eq!(terminals, 1);
    mgr.close("done").await;
}

#[tokio::test]
async fn interrupt_commits_terminal_and_rejects_late_emit() {
    let rejected = Arc::new(Mutex::new(None));
    let mgr = RuntimeThreadManager::new(Arc::new(BlockingRunner {
        post_cancel_emit_rejected: rejected.clone(),
    }));
    let thread = mgr.start("sess-1", "ctx://init").await.unwrap();
    let turn = mgr
        .submit(thread.thread_id.as_str(), "req://1")
        .await
        .unwrap();
    let handle = mgr.get(thread.thread_id.as_str()).await.unwrap();

    // Let the runner reach its cancel await.
    tokio::time::sleep(Duration::from_millis(20)).await;
    mgr.interrupt(thread.thread_id.as_str(), turn.turn_id.as_str(), "stop")
        .await
        .unwrap();

    assert_eq!(wait_for_terminal(&handle).await, "turn_interrupted");
    // The runner's post-cancel emit was rejected (terminal events are the actor's alone).
    tokio::time::sleep(Duration::from_millis(10)).await;
    assert_eq!(*rejected.lock().await, Some(true));
    assert_eq!(handle.state(), ThreadState::Idle);
    mgr.close("done").await;
}

#[tokio::test]
async fn fork_creates_child_from_completed_snapshot() {
    let mgr = RuntimeThreadManager::new(Arc::new(CompletingRunner));
    let thread = mgr.start("sess-1", "ctx://init").await.unwrap();
    let turn = mgr
        .submit(thread.thread_id.as_str(), "req://1")
        .await
        .unwrap();
    let handle = mgr.get(thread.thread_id.as_str()).await.unwrap();
    wait_for_terminal(&handle).await;

    let child = mgr
        .fork(thread.thread_id.as_str(), Some(turn.turn_id.as_str()))
        .await
        .unwrap();
    assert_eq!(
        child.context_ref.as_str(),
        format!("ctx://{}", turn.turn_id.as_str())
    );
    assert_ne!(child.thread_id.as_str(), thread.thread_id.as_str());
    mgr.close("done").await;
}

#[tokio::test]
async fn shutdown_closes_thread_and_rejects_submit() {
    let rejected = Arc::new(Mutex::new(None));
    let mgr = RuntimeThreadManager::new(Arc::new(BlockingRunner {
        post_cancel_emit_rejected: rejected,
    }));
    let thread = mgr.start("sess-1", "ctx://init").await.unwrap();
    mgr.submit(thread.thread_id.as_str(), "req://1")
        .await
        .unwrap();
    let handle = mgr.get(thread.thread_id.as_str()).await.unwrap();

    handle.shutdown("bye").await;
    assert_eq!(handle.state(), ThreadState::Closed);
    let denied = handle
        .start_turn(
            "turn-x".into(),
            athena_types::ArtifactRef::new("req://2").unwrap(),
        )
        .await;
    assert_eq!(denied, Err(RuntimeError::Closed));
    // Idempotent close over an already-closed thread.
    mgr.close("done").await;
}

#[tokio::test]
async fn manager_close_is_idempotent() {
    let mgr = Arc::new(RuntimeThreadManager::new(Arc::new(CompletingRunner)));
    mgr.start("sess-1", "ctx://init").await.unwrap();
    let m2 = mgr.clone();
    let a = tokio::spawn({
        let m = mgr.clone();
        async move { m.close("a").await }
    });
    let b = tokio::spawn(async move { m2.close("b").await });
    a.await.unwrap();
    b.await.unwrap();
    // A start after close is rejected.
    assert_eq!(
        mgr.start("s", "ctx://x").await.err(),
        Some(RuntimeError::Closed)
    );
}
