use crate::transport::TransportClientHalf;
use athena_protocol::{
    ClientMessage, ClientNotification, ErrorCode, EventNotification, RequestEnvelope,
    ResponseEnvelope, RpcError, ServerControlMessage, ServerRequest, method,
};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;
use tokio::sync::{Mutex, mpsc, oneshot};
use tokio::task::JoinHandle;

/// Error returned by the in-process client facade.
#[derive(Debug, Clone)]
pub struct RpcException(RpcError);

impl std::fmt::Display for RpcException {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "RPC error {}: {}", self.0.code, self.0.message)
    }
}

impl std::error::Error for RpcException {}

impl From<RpcError> for RpcException {
    fn from(error: RpcError) -> Self {
        Self(error)
    }
}

/// Monotonic request-id generator. `0` is reserved for initialize.
struct Sequencer {
    next: AtomicU64,
}

impl Sequencer {
    fn new() -> Self {
        Self {
            next: AtomicU64::new(1),
        }
    }

    fn next_id(&self) -> u64 {
        self.next.fetch_add(1, Ordering::SeqCst)
    }
}

/// A message received asynchronously from the server.
#[derive(Debug)]
pub enum ServerEvent {
    Request(ServerRequest),
    Event(EventNotification),
}

type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<ResponseEnvelope>>>>;

/// In-process client facade. Performs the initialize handshake on `start`.
pub struct AthenaClient {
    sender: mpsc::Sender<ClientMessage>,
    pending: Pending,
    control_rx: Mutex<mpsc::Receiver<ServerRequest>>,
    event_rx: Mutex<mpsc::Receiver<EventNotification>>,
    sequencer: Sequencer,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl AthenaClient {
    pub async fn start(client_half: TransportClientHalf) -> Result<Self, RpcException> {
        let TransportClientHalf {
            c2s_tx: sender,
            control_rx: mut inbound_control,
            event_rx: mut inbound_events,
            ready_rx: mut ready,
        } = client_half;
        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        let (control_tx, control_rx) = mpsc::channel(16);
        let (event_tx, event_rx) = mpsc::channel(256);
        let worker_pending = pending.clone();
        let worker = tokio::spawn(async move {
            let mut event_open = true;
            loop {
                tokio::select! {
                    control = inbound_control.recv() => match control {
                        None => break,
                        Some(ServerControlMessage::Response(response)) => {
                            if let Some(tx) = worker_pending.lock().await.remove(&response.request_id) {
                                let _ = tx.send(response);
                            }
                        }
                        Some(ServerControlMessage::ServerRequest(request)) => {
                            let _ = control_tx.send(request).await;
                        }
                    },
                    event = inbound_events.recv(), if event_open => match event {
                        None => event_open = false,
                        Some(event) => { let _ = event_tx.send(event).await; }
                    },
                }
            }
        });

        let client = Self {
            sender,
            pending,
            control_rx: Mutex::new(control_rx),
            event_rx: Mutex::new(event_rx),
            sequencer: Sequencer::new(),
            worker: Mutex::new(Some(worker)),
        };

        client
            .raw_request(
                method::INITIALIZE,
                0,
                json!({
                    "client_name": "athena-cli",
                    "client_version": "0.1.0",
                    "protocol_version": 1,
                }),
                Duration::from_secs(5),
            )
            .await?;
        client.notify(method::INITIALIZED, None).await;

        if !*ready.borrow() {
            let _ = tokio::time::timeout(Duration::from_secs(5), ready.changed()).await;
        }
        Ok(client)
    }

    /// Issue a request and return its result value (or an error).
    pub async fn request(
        &self,
        method: &str,
        params: Value,
        timeout: Duration,
    ) -> Result<Value, RpcException> {
        let rid = self.sequencer.next_id();
        self.raw_request(method, rid, params, timeout).await
    }

    pub async fn notify(&self, method: &str, params: Option<Value>) {
        let _ = self
            .sender
            .send(ClientMessage::Notification(ClientNotification {
                method: method.to_string(),
                params,
            }))
            .await;
    }

    /// Await the next server-pushed event or server request.
    pub async fn next_event(&self, timeout: Duration) -> Option<ServerEvent> {
        let mut control = self.control_rx.lock().await;
        let mut event = self.event_rx.lock().await;
        tokio::select! {
            biased;
            Some(sr) = control.recv() => Some(ServerEvent::Request(sr)),
            Some(ev) = event.recv() => Some(ServerEvent::Event(ev)),
            _ = tokio::time::sleep(timeout) => None,
        }
    }

    /// Request a graceful shutdown and stop the worker.
    pub async fn shutdown(&self, timeout: Duration) {
        let _ = self
            .request(method::SERVER_SHUTDOWN, json!({}), timeout)
            .await;
        self.pending.lock().await.clear();
        if let Some(handle) = self.worker.lock().await.take() {
            handle.abort();
        }
    }

    async fn raw_request(
        &self,
        method: &str,
        request_id: u64,
        params: Value,
        timeout: Duration,
    ) -> Result<Value, RpcException> {
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(request_id, tx);

        if self
            .sender
            .send(ClientMessage::Request(RequestEnvelope {
                request_id,
                method: method.to_string(),
                params: Some(params),
            }))
            .await
            .is_err()
        {
            self.pending.lock().await.remove(&request_id);
            return Err(RpcError::new(ErrorCode::Closed, "transport closed").into());
        }

        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(resp)) => {
                if let Some(err) = resp.error {
                    return Err(err.into());
                }
                Ok(resp.result.unwrap_or(Value::Null))
            }
            _ => {
                self.pending.lock().await.remove(&request_id);
                Err(RpcError::new(ErrorCode::Closed, "request timed out or cancelled").into())
            }
        }
    }
}
