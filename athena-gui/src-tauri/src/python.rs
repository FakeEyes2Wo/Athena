use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::future::Future;
use std::io::BufRead;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{broadcast, oneshot, Mutex};
use tokio_tungstenite::{connect_async, tungstenite::Message};

static NEXT_ID: AtomicU64 = AtomicU64::new(1);
const DEFAULT_RPC_TIMEOUT: Duration = Duration::from_secs(5 * 60);
const LONG_RPC_TIMEOUT: Duration = Duration::from_secs(30 * 60);

type PendingRequests = Arc<Mutex<HashMap<u64, oneshot::Sender<RpcResult<Value>>>>>;

fn fail_pending(pending: &mut HashMap<u64, oneshot::Sender<RpcResult<Value>>>, message: &str) {
    for (_, sender) in pending.drain() {
        let _ = sender.send(Err(RpcError::transport(message)));
    }
}

struct PendingRequestGuard {
    pending: PendingRequests,
    request_id: u64,
}

impl Drop for PendingRequestGuard {
    fn drop(&mut self) {
        if let Ok(mut pending) = self.pending.try_lock() {
            pending.remove(&self.request_id);
            return;
        }
        let pending = self.pending.clone();
        let request_id = self.request_id;
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                pending.lock().await.remove(&request_id);
            });
        }
    }
}

async fn send_and_await<F>(
    request_id: u64,
    pending: &PendingRequests,
    timeout: Duration,
    send: F,
    response: oneshot::Receiver<RpcResult<Value>>,
) -> RpcResult<Value>
where
    F: Future<Output = RpcResult<()>>,
{
    let operation = async {
        send.await?;
        match response.await {
            Ok(result) => result,
            Err(_) => Err(RpcError::transport("Request cancelled")),
        }
    };
    match tokio::time::timeout(timeout, operation).await {
        Ok(result) => {
            if result.is_err() {
                pending.lock().await.remove(&request_id);
            }
            result
        }
        Err(_) => {
            pending.lock().await.remove(&request_id);
            Err(RpcError::transport("RPC request timed out"))
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone, PartialEq)]
pub struct RpcError {
    pub code: i64,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

impl RpcError {
    pub fn transport(message: impl Into<String>) -> Self {
        Self {
            code: -32000,
            message: message.into(),
            data: None,
        }
    }
}

impl std::fmt::Display for RpcError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

pub type RpcResult<T> = Result<T, RpcError>;

fn decode_response(response: Value) -> RpcResult<Value> {
    if let Some(error) = response.get("error").filter(|value| !value.is_null()) {
        let error = serde_json::from_value(error.clone())
            .map_err(|error| RpcError::transport(format!("invalid RPC error: {error}")))?;
        return Err(error);
    }
    Ok(response.get("result").cloned().unwrap_or(Value::Null))
}

#[derive(Debug, Deserialize, Clone)]
pub struct EventNotification {
    pub kind: String,
    pub data: Value,
}

pub struct PythonBridge {
    child: Mutex<Child>,
    ws_send: Mutex<
        futures_util::stream::SplitSink<
            tokio_tungstenite::WebSocketStream<
                tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
            >,
            Message,
        >,
    >,
    pending: PendingRequests,
    connection_closed: Arc<AtomicBool>,
    events: broadcast::Sender<EventNotification>,
}

impl PythonBridge {
    /// Spawn the Python GUI gateway, connect via WebSocket, and return a ready bridge.
    ///
    /// Release 模式下优先使用打包进 resources 的 ``gui_gateway(.exe)``；找不到时
    /// 退回开发模式 ``uv run python -m gui_gateway``。
    pub async fn start(resource_dir: Option<std::path::PathBuf>) -> Result<Self, String> {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest_dir
            .parent() // src-tauri -> athena-gui
            .and_then(|p| p.parent()) // athena-gui -> repo root
            .unwrap_or(manifest_dir);

        let exe_name = if cfg!(windows) {
            "gui_gateway.exe"
        } else {
            "gui_gateway"
        };
        let bundled = resource_dir
            .as_ref()
            .map(|rd| rd.join(exe_name))
            .filter(|p| p.is_file());

        let mut child = if let Some(exe) = bundled {
            Command::new(&exe)
                .current_dir(repo_root)
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .spawn()
                .map_err(|e| format!("Failed to spawn bundled gateway: {}", e))?
        } else {
            Command::new("uv")
                .args(["run", "python", "-m", "gui_gateway"])
                .env("ATHENA_GUI_PORT", "0")
                .current_dir(repo_root)
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .spawn()
                .map_err(|e| format!("Failed to spawn Python: {}", e))?
        };

        // 2. Read port from first line of stdout
        let stdout = child.stdout.take().ok_or("No stdout")?;
        let mut reader = std::io::BufReader::new(stdout);
        let port = read_gateway_port(&mut reader)?;

        // Spawn a blocking task to keep draining stdout (subsequent lines are logs)
        tokio::task::spawn_blocking(move || {
            let mut line = String::new();
            loop {
                line.clear();
                match reader.read_line(&mut line) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {} // drain, don't log to avoid noise
                }
            }
        });

        // 3. Connect WebSocket
        let url = format!("ws://127.0.0.1:{}", port);
        let (ws, _) = connect_async(&url)
            .await
            .map_err(|e| format!("WS connect failed: {}", e))?;
        let (ws_send, mut ws_recv) = ws.split();
        let ws_send = Mutex::new(ws_send);

        let (events_tx, _) = broadcast::channel(256);
        let events_clone = events_tx.clone();

        // 4. Spawn receive loop that dispatches responses and events
        let pending: PendingRequests = Arc::new(Mutex::new(HashMap::new()));
        let pending_recv = pending.clone();
        let connection_closed = Arc::new(AtomicBool::new(false));
        let connection_closed_recv = connection_closed.clone();
        tokio::spawn(async move {
            while let Some(msg) = ws_recv.next().await {
                match msg {
                    Ok(Message::Text(text)) => {
                        if let Ok(val) = serde_json::from_str::<Value>(&text) {
                            if let Some(rid) = val.get("request_id").and_then(|v| v.as_u64()) {
                                // Response to a pending request
                                let tx = {
                                    let mut p = pending_recv.lock().await;
                                    p.remove(&rid)
                                };
                                if let Some(tx) = tx {
                                    let _ = tx.send(decode_response(val));
                                }
                            } else if val.get("kind").is_some() {
                                // Event notification
                                if let Ok(evt) = serde_json::from_value::<EventNotification>(val) {
                                    let _ = events_clone.send(evt);
                                }
                            }
                        }
                    }
                    Ok(Message::Close(_)) => break,
                    Err(_) => break,
                    _ => {}
                }
            }
            // Serialize closure with request registration so no request can be
            // inserted after the receiver has drained the pending map.
            let mut pending = pending_recv.lock().await;
            connection_closed_recv.store(true, Ordering::Release);
            fail_pending(&mut pending, "Python gateway connection closed");
        });

        Ok(Self {
            child: Mutex::new(child),
            ws_send,
            pending,
            connection_closed,
            events: events_tx,
        })
    }

    /// Send an RPC call and await the response.
    pub async fn call_rpc(&self, method: &str, params: Value) -> RpcResult<Value> {
        let id = NEXT_ID.fetch_add(1, Ordering::SeqCst);
        let req = serde_json::json!({
            "request_id": id,
            "method": method,
            "params": params,
        });
        let (tx, rx) = oneshot::channel();
        {
            let mut pending = self.pending.lock().await;
            if self.connection_closed.load(Ordering::Acquire) {
                return Err(RpcError::transport("Python gateway connection closed"));
            }
            pending.insert(id, tx);
        }
        let _pending_guard = PendingRequestGuard {
            pending: self.pending.clone(),
            request_id: id,
        };
        let send = async {
            self.ws_send
                .lock()
                .await
                .send(Message::Text(req.to_string()))
                .await
                .map(|_| ())
                .map_err(|error| RpcError::transport(format!("WS send failed: {error}")))
        };
        let timeout = match method {
            "generate_report" | "start_validation" => LONG_RPC_TIMEOUT,
            _ => DEFAULT_RPC_TIMEOUT,
        };
        send_and_await(id, &self.pending, timeout, send, rx).await
    }

    pub async fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        self.call_rpc(method, params)
            .await
            .map_err(|error| error.to_string())
    }

    /// Subscribe to event notifications from the Python backend.
    pub fn subscribe(&self) -> broadcast::Receiver<EventNotification> {
        self.events.subscribe()
    }
}

fn read_gateway_port(reader: &mut impl BufRead) -> Result<u16, String> {
    let mut line = String::new();
    loop {
        line.clear();
        let bytes = reader
            .read_line(&mut line)
            .map_err(|error| format!("Failed to read gateway port: {error}"))?;
        if bytes == 0 {
            return Err("Gateway exited before publishing its port".to_string());
        }
        if let Ok(port) = line.trim().parse() {
            return Ok(port);
        }
    }
}

impl Drop for PythonBridge {
    fn drop(&mut self) {
        // Terminate the spawned Python subprocess on bridge teardown.
        if let Ok(mut child) = self.child.try_lock() {
            let _ = child.kill();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn deserializes_minimal_event() {
        let event: EventNotification = serde_json::from_value(json!({
            "kind": "output",
            "data": {"text": "hello"}
        }))
        .expect("deserialize event");
        assert_eq!(event.kind, "output");
        assert_eq!(event.data["text"], "hello");
    }

    #[test]
    fn decodes_success_and_structured_error() {
        let result = decode_response(json!({
            "result": {"ok": true},
            "error": null
        }))
        .expect("successful response");
        assert_eq!(result, json!({"ok": true}));

        let error = decode_response(json!({
            "error": {
                "code": -32602,
                "message": "stale revision",
                "data": {"code": "stale_revision"}
            }
        }))
        .expect_err("domain error");
        assert_eq!(error.code, -32602);
        assert_eq!(error.data.unwrap()["code"], "stale_revision");
    }

    #[test]
    fn blocked_send_times_out_and_removes_its_pending_sender() {
        tauri::async_runtime::block_on(async {
            let pending: PendingRequests = Arc::new(Mutex::new(HashMap::new()));
            let (sender, receiver) = oneshot::channel();
            pending.lock().await.insert(7, sender);

            let (send_gate, send_blocker) = oneshot::channel::<()>();
            let send = async move {
                send_blocker
                    .await
                    .map_err(|_| RpcError::transport("test send gate closed"))?;
                Ok(())
            };
            let error = send_and_await(7, &pending, Duration::from_millis(1), send, receiver)
                .await
                .expect_err("blocked send should time out");
            drop(send_gate);

            assert_eq!(error.message, "RPC request timed out");
            assert!(pending.lock().await.is_empty());
        });
    }

    #[test]
    fn dropped_rpc_future_guard_removes_its_pending_sender() {
        tauri::async_runtime::block_on(async {
            let pending: PendingRequests = Arc::new(Mutex::new(HashMap::new()));
            let (sender, receiver) = oneshot::channel();
            pending.lock().await.insert(8, sender);

            drop(PendingRequestGuard {
                pending: pending.clone(),
                request_id: 8,
            });

            assert!(pending.lock().await.is_empty());
            assert!(receiver.await.is_err());
        });
    }

    #[test]
    fn receiver_shutdown_fails_all_pending_rpcs() {
        tauri::async_runtime::block_on(async {
            let pending: PendingRequests = Arc::new(Mutex::new(HashMap::new()));
            let (sender, receiver) = oneshot::channel();
            pending.lock().await.insert(9, sender);
            {
                let mut pending = pending.lock().await;
                fail_pending(&mut pending, "Python gateway connection closed");
            }

            let error = receiver
                .await
                .expect("pending call is awakened")
                .unwrap_err();
            assert_eq!(error.message, "Python gateway connection closed");
            assert!(pending.lock().await.is_empty());
        });
    }

    #[test]
    fn real_python_gateway_completes_a_native_rpc_round_trip() {
        tauri::async_runtime::block_on(async {
            let bridge = PythonBridge::start(None)
                .await
                .expect("start the production Python gateway");
            let state = bridge
                .call_rpc("state_get", json!({}))
                .await
                .expect("receive a structured state response");

            assert!(state.get("status").is_some());
            assert!(state.get("phase").is_some());
        });
    }
}
