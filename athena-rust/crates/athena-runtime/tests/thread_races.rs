//! Runs a completing turn against a concurrent interrupt 1000 times and asserts
//! exactly one terminal event is committed every time.
#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use athena_runtime::RuntimeThreadManager;
use common::{RacyRunner, is_terminal};
use std::sync::Arc;
use std::sync::atomic::AtomicUsize;
use std::time::Duration;

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn complete_interrupt_race_yields_single_terminal() {
    let count = Arc::new(AtomicUsize::new(0));
    let mgr = Arc::new(RuntimeThreadManager::new(Arc::new(RacyRunner {
        count: count.clone(),
    })));

    for i in 0..1000 {
        let thread = mgr.start("sess", "ctx://0").await.unwrap();
        let tid = thread.thread_id.as_str().to_string();
        let turn = mgr.submit(&tid, "req://1").await.unwrap();
        let turn_id = turn.turn_id.as_str().to_string();

        let interrupt = tokio::spawn({
            let m = mgr.clone();
            let tid = tid.clone();
            async move {
                let _ = m.interrupt(&tid, &turn_id).await;
            }
        });
        interrupt.await.unwrap();

        let handle = mgr.get(&tid).await.unwrap();
        let mut terminals = 0;
        for _ in 0..1000 {
            terminals = handle
                .journal()
                .snapshot()
                .iter()
                .filter(|e| is_terminal(&e.kind))
                .count();
            if terminals >= 1 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        assert_eq!(terminals, 1, "iteration {i}: expected exactly one terminal");
        handle.shutdown().await;
    }

    mgr.close().await;
}
