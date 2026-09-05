use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::io::BufRead;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::{broadcast, oneshot, Mutex};
use tokio_tungstenite::{connect_async, tungstenite::Message};

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

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
    pending: Arc<Mutex<HashMap<u64, oneshot::Sender<RpcResult<Value>>>>>,
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
        let pending: Arc<Mutex<HashMap<u64, oneshot::Sender<RpcResult<Value>>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let pending_recv = pending.clone();
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
        });

        Ok(Self {
            child: Mutex::new(child),
            ws_send,
            pending,
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
        self.pending.lock().await.insert(id, tx);
        self.ws_send
            .lock()
            .await
            .send(Message::Text(req.to_string()))
            .await
            .map_err(|e| RpcError::transport(format!("WS send failed: {e}")))?;
        match rx.await {
            Ok(Ok(value)) => Ok(value),
            Ok(Err(error)) => Err(error),
            Err(_) => Err(RpcError::transport("Request cancelled")),
        }
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
