use crate::execution::Executor;
use crate::transport::TransportServerHalf;
use athena_protocol::{
    ClientMessage, ErrorCode, RequestEnvelope, ResponseEnvelope, RpcError, method,
};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};
use std::time::Duration;
use tokio::sync::{Mutex, Semaphore};
use tokio::task::JoinHandle;

pub const INITIALIZING: u8 = 1;
pub const READY: u8 = 2;
pub const DRAINING: u8 = 3;
pub const TERMINATED: u8 = 4;

const RESERVED_INIT_ID: u64 = 0;

/// The server-side request entrypoint: a state machine + admission gate that
/// delegates business methods to an [`Executor`].
pub struct MessageProcessor {
    state: Arc<AtomicU8>,
    inflight: Arc<Mutex<HashSet<u64>>>,
    dispatch: Mutex<Option<JoinHandle<()>>>,
}

impl MessageProcessor {
    /// Start the dispatch loop over the server transport half.
    pub fn start(
        server: TransportServerHalf,
        executor: Arc<dyn Executor>,
        max_inflight: usize,
    ) -> Self {
        let state = Arc::new(AtomicU8::new(INITIALIZING));
        let inflight = Arc::new(Mutex::new(HashSet::new()));
        let slots = Arc::new(Semaphore::new(max_inflight.max(1)));
        let dispatch = tokio::spawn(dispatch_loop(
            server,
            executor,
            state.clone(),
            inflight.clone(),
            slots,
        ));
        Self {
            state,
            inflight,
            dispatch: Mutex::new(Some(dispatch)),
        }
    }

    pub fn state(&self) -> u8 {
        self.state.load(Ordering::SeqCst)
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
        self.state.store(TERMINATED, Ordering::SeqCst);
        if let Some(handle) = self.dispatch.lock().await.take() {
            handle.abort();
        }
    }
}

async fn dispatch_loop(
    mut server: TransportServerHalf,
    executor: Arc<dyn Executor>,
    state: Arc<AtomicU8>,
    inflight: Arc<Mutex<HashSet<u64>>>,
    slots: Arc<Semaphore>,
) {
    let responder = server.responder();
    loop {
        let Some(msg) = server.recv().await else {
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
                    let _ = responder
                        .send_response(ResponseEnvelope {
                            request_id: rid,
                            result: Some(json!({ "status": "shutting_down" })),
                            error: None,
                        })
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
                    let _ = responder
                        .send_response(ResponseEnvelope {
                            request_id: rid,
                            result: None,
                            error: Some(err),
                        })
                        .await;
                    continue;
                }

                {
                    let mut inf = inflight.lock().await;
                    if inf.contains(&rid) {
                        let _ = responder
                            .send_response(ResponseEnvelope {
                                request_id: rid,
                                result: None,
                                error: Some(RpcError::new(
                                    ErrorCode::DuplicateRequestId,
                                    "duplicate request id",
                                )),
                            })
                            .await;
                        continue;
                    }
                    inf.insert(rid);
                }

                let Ok(permit) = slots.clone().acquire_owned().await else {
                    inflight.lock().await.remove(&rid);
                    continue;
                };
                let executor = executor.clone();
                let responder = responder.clone();
                let inflight = inflight.clone();
                let state = state.clone();
                tokio::spawn(async move {
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
                    inflight.lock().await.remove(&rid);
                    drop(permit);
                });
            }
        }
    }
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
