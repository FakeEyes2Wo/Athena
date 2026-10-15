use crate::execution::Executor;
use crate::transport::{ServerResponder, TransportServerHalf};
use athena_protocol::{
    ClientMessage, ErrorCode, RequestEnvelope, ResponseEnvelope, RpcError, method,
};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};
use std::time::Duration;
use tokio::sync::Mutex;
use tokio::task::{JoinError, JoinHandle, JoinSet};

const INITIALIZING: u8 = 1;
const READY: u8 = 2;
const DRAINING: u8 = 3;
const TERMINATED: u8 = 4;

const RESERVED_INIT_ID: u64 = 0;
const MAX_INFLIGHT: usize = 64;
const DISPATCH_STOP_GRACE: Duration = Duration::from_millis(100);

/// The server-side request entrypoint: a state machine + admission gate that
/// delegates business methods to an [`Executor`].
pub struct MessageProcessor {
    state: Arc<AtomicU8>,
    inflight: Arc<Mutex<HashSet<u64>>>,
    stop_tx: tokio::sync::watch::Sender<bool>,
    dispatch: Mutex<Option<JoinHandle<()>>>,
}

impl MessageProcessor {
    /// Start the dispatch loop over the server transport half.
    pub fn start(server: TransportServerHalf, executor: Arc<dyn Executor>) -> Self {
        let state = Arc::new(AtomicU8::new(INITIALIZING));
        let inflight = Arc::new(Mutex::new(HashSet::new()));
        let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
        let dispatch = tokio::spawn(dispatch_loop(
            server,
            executor,
            state.clone(),
            inflight.clone(),
            stop_rx,
        ));
        Self {
            state,
            inflight,
            stop_tx,
            dispatch: Mutex::new(Some(dispatch)),
        }
    }

    /// Drain in-flight requests (up to `timeout`), then terminate.
    pub async fn shutdown(&self, timeout: Duration) {
        let s = self.state.load(Ordering::SeqCst);
        if s == TERMINATED {
            return;
        }
        self.state.store(DRAINING, Ordering::SeqCst);
        let deadline = tokio::time::Instant::now() + timeout;
        while !self.inflight.lock().await.is_empty() {
            if tokio::time::Instant::now() >= deadline {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        self.stop_tx.send_replace(true);
        let mut dispatch = self.dispatch.lock().await;
        if let Some(handle) = dispatch.as_mut()
            && tokio::time::timeout(DISPATCH_STOP_GRACE, &mut *handle)
                .await
                .is_err()
        {
            // If stop cannot reach the dispatcher, abort it as a bounded fallback.
            // Dropping its JoinSet requests child cancellation but cannot join those children.
            handle.abort();
            let _ = handle.await;
        }
        dispatch.take();
        // The dispatcher exited; child tasks are joined on its normal stop branch.
        self.state.store(TERMINATED, Ordering::SeqCst);
    }
}

impl Drop for MessageProcessor {
    fn drop(&mut self) {
        self.stop_tx.send_replace(true);
        if let Some(dispatch) = self.dispatch.get_mut().take() {
            // Drop cannot await shutdown; abort so a blocked response send cannot detach the loop.
            dispatch.abort();
        }
    }
}

async fn dispatch_loop(
    mut server: TransportServerHalf,
    executor: Arc<dyn Executor>,
    state: Arc<AtomicU8>,
    inflight: Arc<Mutex<HashSet<u64>>>,
    mut stop_rx: tokio::sync::watch::Receiver<bool>,
) {
    let responder = server.responder();
    let mut handlers = JoinSet::new();
    let mut handler_ids = HashMap::new();
    loop {
        let msg = tokio::select! {
            _ = stop_rx.changed() => break,
            finished = handlers.join_next_with_id(), if !handlers.is_empty() => {
                if let Some(result) = finished {
                    cleanup_handler(result, &mut handler_ids, &inflight).await;
                }
                continue;
            }
            msg = server.recv() => msg,
        };
        let Some(msg) = msg else {
            state.store(DRAINING, Ordering::SeqCst);
            break;
        };
        match msg {
            ClientMessage::Notification(n) => {
                if n.method == method::INITIALIZED && state.load(Ordering::SeqCst) == INITIALIZING {
                    state.store(READY, Ordering::SeqCst);
                    server.set_ready();
                }
            }
            ClientMessage::ServerRequestReply(_) => {
                // Server-initiated approval replies are out of scope for this migration.
            }
            ClientMessage::Request(req) => {
                let rid = req.request_id;

                if req.method == method::SERVER_SHUTDOWN {
                    let s = state.load(Ordering::SeqCst);
                    if s != DRAINING && s != TERMINATED {
                        state.store(DRAINING, Ordering::SeqCst);
                    }
                    let _ = send_response_or_stop(
                        &responder,
                        ResponseEnvelope {
                            request_id: rid,
                            result: Some(json!({ "status": "shutting_down" })),
                            error: None,
                        },
                        &mut stop_rx,
                    )
                    .await;
                    break;
                }

                if !method::is_control_method(&req.method) && state.load(Ordering::SeqCst) != READY
                {
                    let err = match state.load(Ordering::SeqCst) {
                        INITIALIZING => {
                            RpcError::new(ErrorCode::NotInitialized, "server not initialized")
                        }
                        DRAINING => RpcError::new(ErrorCode::Closed, "server is draining"),
                        _ => RpcError::new(ErrorCode::Closed, "server closed"),
                    };
                    if !send_response_or_stop(
                        &responder,
                        ResponseEnvelope {
                            request_id: rid,
                            result: None,
                            error: Some(err),
                        },
                        &mut stop_rx,
                    )
                    .await
                    {
                        break;
                    }
                    continue;
                }

                let admission = {
                    let mut inf = inflight.lock().await;
                    if inf.contains(&rid) {
                        Err(RpcError::new(
                            ErrorCode::DuplicateRequestId,
                            "duplicate request id",
                        ))
                    } else if handlers.len() >= MAX_INFLIGHT {
                        Err(RpcError::new(
                            ErrorCode::Overloaded,
                            "server request capacity reached",
                        ))
                    } else {
                        inf.insert(rid);
                        Ok(())
                    }
                };
                if let Err(error) = admission {
                    if !send_response_or_stop(
                        &responder,
                        ResponseEnvelope {
                            request_id: rid,
                            result: None,
                            error: Some(error),
                        },
                        &mut stop_rx,
                    )
                    .await
                    {
                        break;
                    }
                    continue;
                }
                let executor = executor.clone();
                let responder = responder.clone();
                let state = state.clone();
                let handler = handlers.spawn(async move {
                    let result = if req.method == method::INITIALIZE {
                        handle_initialize(&req, &state)
                    } else {
                        executor
                            .execute(&req.method, req.params.clone().unwrap_or_else(|| json!({})))
                            .await
                    };
                    let envelope = match result {
                        Ok(value) => ResponseEnvelope {
                            request_id: rid,
                            result: Some(value),
                            error: None,
                        },
                        Err(err) => ResponseEnvelope {
                            request_id: rid,
                            result: None,
                            error: Some(err),
                        },
                    };
                    let _ = responder.send_response(envelope).await;
                    rid
                });
                handler_ids.insert(handler.id(), rid);
            }
        }
    }

    // An external stop aborts and joins handlers; protocol shutdown drains them normally.
    if *stop_rx.borrow() {
        handlers.abort_all();
    }
    while !handlers.is_empty() {
        tokio::select! {
            _ = stop_rx.changed(), if !*stop_rx.borrow() => handlers.abort_all(),
            result = handlers.join_next_with_id() => {
                if let Some(result) = result {
                    cleanup_handler(result, &mut handler_ids, &inflight).await;
                }
            }
        }
    }
    if *stop_rx.borrow() {
        inflight.lock().await.clear();
    }
}

async fn send_response_or_stop(
    responder: &ServerResponder,
    response: ResponseEnvelope,
    stop_rx: &mut tokio::sync::watch::Receiver<bool>,
) -> bool {
    tokio::select! {
        biased;
        _ = stop_rx.changed() => false,
        _ = responder.send_response(response) => true,
    }
}

async fn cleanup_handler(
    result: Result<(tokio::task::Id, u64), JoinError>,
    handler_ids: &mut HashMap<tokio::task::Id, u64>,
    inflight: &Mutex<HashSet<u64>>,
) {
    let request_id = match result {
        Ok((task_id, request_id)) => {
            handler_ids.remove(&task_id);
            request_id
        }
        Err(error) => handler_ids.remove(&error.id()).unwrap_or_default(),
    };
    inflight.lock().await.remove(&request_id);
}

fn handle_initialize(req: &RequestEnvelope, state: &AtomicU8) -> Result<Value, RpcError> {
    if req.request_id != RESERVED_INIT_ID {
        return Err(RpcError::new(
            ErrorCode::InvalidArgument,
            "initialize must use request_id=0",
        ));
    }
    if state.load(Ordering::SeqCst) != INITIALIZING {
        return Err(RpcError::new(
            ErrorCode::AlreadyInitialized,
            "already initialized",
        ));
    }
    let params = req.params.clone().unwrap_or_else(|| json!({}));
    let version = params
        .get("protocol_version")
        .and_then(|v| v.as_u64())
        .unwrap_or(1);
    if version != 1 {
        return Err(RpcError::new(
            ErrorCode::InvalidArgument,
            "unsupported protocol version",
        ));
    }
    Ok(json!({ "protocol_version": 1, "server_name": "athena", "status": "ok" }))
}

#[cfg(test)]
#[allow(clippy::expect_used)]
mod tests {
    use super::*;
    use crate::execution::Executor;
    use crate::transport;
    use athena_protocol::{
        ClientMessage, ClientNotification, RequestEnvelope, ServerControlMessage,
    };
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::sync::Notify;

    struct PendingExecutor {
        started: AtomicUsize,
        cancelled: Arc<AtomicUsize>,
        release: Arc<Notify>,
    }

    struct CancellationMarker(Arc<AtomicUsize>);

    impl Drop for CancellationMarker {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }

    #[async_trait::async_trait]
    impl Executor for PendingExecutor {
        async fn execute(&self, _method: &str, _params: Value) -> Result<Value, RpcError> {
            self.started.fetch_add(1, Ordering::SeqCst);
            let _marker = CancellationMarker(self.cancelled.clone());
            self.release.notified().await;
            Ok(json!({"status": "released"}))
        }
    }

    async fn receive_response(client: &mut crate::transport::TransportClientHalf, id: u64) {
        loop {
            let Some(ServerControlMessage::Response(response)) = client.recv_control().await else {
                panic!("expected response {id}");
            };
            if response.request_id == id {
                return;
            }
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn saturated_dispatch_still_accepts_shutdown_and_cancels_owned_handlers() {
        let (mut client, server) = transport();
        let cancelled = Arc::new(AtomicUsize::new(0));
        let executor = Arc::new(PendingExecutor {
            started: AtomicUsize::new(0),
            cancelled: cancelled.clone(),
            release: Arc::new(Notify::new()),
        });
        let processor = MessageProcessor::start(server, executor.clone());

        client
            .send(ClientMessage::Request(RequestEnvelope {
                request_id: 0,
                method: method::INITIALIZE.into(),
                params: Some(json!({"protocol_version": 1})),
            }))
            .await
            .expect("send initialize");
        receive_response(&mut client, 0).await;
        client
            .send(ClientMessage::Notification(ClientNotification {
                method: method::INITIALIZED.into(),
                params: None,
            }))
            .await
            .expect("send initialized");

        for request_id in 1..=MAX_INFLIGHT as u64 {
            client
                .send(ClientMessage::Request(RequestEnvelope {
                    request_id,
                    method: "held".into(),
                    params: None,
                }))
                .await
                .expect("send held request");
        }
        tokio::time::timeout(Duration::from_secs(1), async {
            while executor.started.load(Ordering::SeqCst) < MAX_INFLIGHT {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("all request slots should be occupied");

        client
            .send(ClientMessage::Request(RequestEnvelope {
                request_id: 1000,
                method: "held".into(),
                params: None,
            }))
            .await
            .expect("send request beyond capacity");
        client
            .send(ClientMessage::Request(RequestEnvelope {
                request_id: 1001,
                method: method::SERVER_SHUTDOWN.into(),
                params: None,
            }))
            .await
            .expect("send shutdown behind saturated requests");

        tokio::time::timeout(Duration::from_millis(200), async {
            let mut saw_overload = false;
            let mut saw_shutdown = false;
            while !saw_shutdown {
                let Some(ServerControlMessage::Response(response)) = client.recv_control().await
                else {
                    panic!("response channel closed");
                };
                if response.request_id == 1000 {
                    saw_overload = response
                        .error
                        .as_ref()
                        .is_some_and(|error| error.code == ErrorCode::Overloaded as i64);
                }
                saw_shutdown = response.request_id == 1001;
            }
            assert!(saw_overload, "excess requests must receive OVERLOADED");
        })
        .await
        .expect("shutdown must bypass full executor capacity");

        processor.shutdown(Duration::from_millis(10)).await;
        tokio::time::timeout(Duration::from_millis(200), async {
            while cancelled.load(Ordering::SeqCst) < MAX_INFLIGHT {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("shutdown should cancel and join every executor task");
    }

    #[tokio::test]
    async fn repeated_shutdown_waits_for_backpressured_handler_cleanup() {
        let (client, server) = transport();
        let cancelled = Arc::new(AtomicUsize::new(0));
        let executor = Arc::new(PendingExecutor {
            started: AtomicUsize::new(0),
            cancelled: cancelled.clone(),
            release: Arc::new(Notify::new()),
        });
        let processor = Arc::new(MessageProcessor::start(server, executor.clone()));

        // Fill the bounded control lane with pre-initialization responses.
        for request_id in 1..=MAX_INFLIGHT as u64 {
            client
                .send(ClientMessage::Request(RequestEnvelope {
                    request_id,
                    method: "not_ready".into(),
                    params: None,
                }))
                .await
                .expect("send pre-initialization request");
        }
        client
            .send(ClientMessage::Request(RequestEnvelope {
                request_id: RESERVED_INIT_ID,
                method: method::INITIALIZE.into(),
                params: Some(json!({"protocol_version": 1})),
            }))
            .await
            .expect("send initialize");
        client
            .send(ClientMessage::Notification(ClientNotification {
                method: method::INITIALIZED.into(),
                params: None,
            }))
            .await
            .expect("send initialized");
        tokio::time::timeout(Duration::from_secs(1), async {
            while processor.state.load(Ordering::SeqCst) != READY {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the response lane should be full before initialization completes");
        assert_eq!(client.control_rx.len(), MAX_INFLIGHT);

        client
            .send(ClientMessage::Request(RequestEnvelope {
                request_id: 1000,
                method: "held".into(),
                params: None,
            }))
            .await
            .expect("send held request");
        tokio::time::timeout(Duration::from_secs(1), async {
            while executor.started.load(Ordering::SeqCst) != 1 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("executor handler should start");

        client
            .send(ClientMessage::Request(RequestEnvelope {
                request_id: 1001,
                method: method::SERVER_SHUTDOWN.into(),
                params: None,
            }))
            .await
            .expect("send protocol shutdown into the full response lane");
        let shutdown_processor = processor.clone();
        let shutdown =
            tokio::spawn(async move { shutdown_processor.shutdown(Duration::ZERO).await });
        tokio::time::timeout(Duration::from_secs(1), async {
            while !*processor.stop_tx.borrow() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the first shutdown should reach its dispatcher join");
        let repeated_processor = processor.clone();
        let repeated_cancelled = cancelled.clone();
        let repeated = tokio::spawn(async move {
            repeated_processor.shutdown(Duration::ZERO).await;
            repeated_cancelled.load(Ordering::SeqCst)
        });
        let observed_drops = tokio::time::timeout(Duration::from_secs(1), repeated)
            .await
            .expect("repeated shutdown should finish with the active shutdown")
            .expect("repeated shutdown task should finish");
        assert_eq!(
            observed_drops, 1,
            "repeated shutdown must await handler cleanup"
        );
        shutdown.await.expect("first shutdown task should finish");

        assert_eq!(
            cancelled.load(Ordering::SeqCst),
            1,
            "shutdown must finish dropping the handler before it returns"
        );
    }

    #[tokio::test]
    async fn dropping_processor_aborts_dispatch_blocked_on_response_send() {
        let (client, server) = transport();
        let cancelled = Arc::new(AtomicUsize::new(0));
        let executor = Arc::new(PendingExecutor {
            started: AtomicUsize::new(0),
            cancelled: cancelled.clone(),
            release: Arc::new(Notify::new()),
        });
        let processor = MessageProcessor::start(server, executor.clone());

        for request_id in 1..=MAX_INFLIGHT as u64 {
            client
                .send(ClientMessage::Request(RequestEnvelope {
                    request_id,
                    method: "not_ready".into(),
                    params: None,
                }))
                .await
                .expect("send pre-initialization request");
        }
        client
            .send(ClientMessage::Request(RequestEnvelope {
                request_id: RESERVED_INIT_ID,
                method: method::INITIALIZE.into(),
                params: Some(json!({"protocol_version": 1})),
            }))
            .await
            .expect("send initialize");
        client
            .send(ClientMessage::Notification(ClientNotification {
                method: method::INITIALIZED.into(),
                params: None,
            }))
            .await
            .expect("send initialized");
        tokio::time::timeout(Duration::from_secs(1), async {
            while processor.state.load(Ordering::SeqCst) != READY {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the response lane should be full before initialization completes");
        assert_eq!(client.control_rx.len(), MAX_INFLIGHT);

        client
            .send(ClientMessage::Request(RequestEnvelope {
                request_id: 1000,
                method: "held".into(),
                params: None,
            }))
            .await
            .expect("send held request");
        tokio::time::timeout(Duration::from_secs(1), async {
            while executor.started.load(Ordering::SeqCst) != 1 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("executor handler should start");
        client
            .send(ClientMessage::Request(RequestEnvelope {
                request_id: 1001,
                method: method::SERVER_SHUTDOWN.into(),
                params: None,
            }))
            .await
            .expect("send protocol shutdown into the full response lane");
        tokio::time::timeout(Duration::from_secs(1), async {
            while processor.state.load(Ordering::SeqCst) != DRAINING {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("dispatch should reach the blocked shutdown response");

        drop(processor);
        tokio::time::timeout(Duration::from_millis(200), async {
            while cancelled.load(Ordering::SeqCst) != 1 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("dropping the processor must abort its dispatcher and handlers");
    }
}
