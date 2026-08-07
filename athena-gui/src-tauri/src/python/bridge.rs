use std::collections::HashMap;
use std::io::BufRead;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::{broadcast, oneshot, Mutex};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use crate::python::types::*;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

pub struct PythonBridge {
    child: Mutex<Child>,
    ws_send: Mutex<futures_util::stream::SplitSink<
        tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>
        >,
        Message,
    >>,
    pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>>,
    events: broadcast::Sender<EventNotification>,
}

impl PythonBridge {
    /// Spawn the Python GUI gateway, connect via WebSocket, and return a ready bridge.
    pub async fn start() -> Result<Self, String> {
        // 1. Spawn Python process from repo root.
        //    CARGO_MANIFEST_DIR = <repo>/athena-gui/src-tauri
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest_dir
            .parent() // src-tauri -> athena-gui
            .and_then(|p| p.parent()) // athena-gui -> repo root
            .unwrap_or(manifest_dir);
        let mut child = Command::new("uv")
            .args(["run", "python", "-m", "gui_gateway"])
            .current_dir(&repo_root)
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| format!("Failed to spawn Python: {}", e))?;

        // 2. Read port from first line of stdout
        let stdout = child.stdout.take().ok_or("No stdout")?;
        let mut reader = std::io::BufReader::new(stdout);
        let mut port_line = String::new();
        reader
            .read_line(&mut port_line)
            .map_err(|e| format!("Failed to read port: {}", e))?;
        let port: u16 = port_line
            .trim()
            .parse()
            .map_err(|e| format!("Invalid port: {}", e))?;

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
        let pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let pending_recv = pending.clone();
        tokio::spawn(async move {
            while let Some(msg) = ws_recv.next().await {
                match msg {
                    Ok(Message::Text(text)) => {
                        if let Ok(val) = serde_json::from_str::<Value>(&text) {
                            if let Some(rid) = val
                                .get("request_id")
                                .and_then(|v| v.as_u64())
                            {
                                // Response to a pending request
                                let tx = {
                                    let mut p = pending_recv.lock().await;
                                    p.remove(&rid)
                                };
                                if let Some(tx) = tx {
                                    let _ = tx.send(
                                        val.get("result")
                                            .cloned()
                                            .unwrap_or(Value::Null),
                                    );
                                }
                            } else if val.get("kind").is_some() {
                                // Event notification
                                if let Ok(evt) =
                                    serde_json::from_value::<EventNotification>(val)
                                {
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
    pub async fn call(&self, method: &str, params: Value) -> Result<Value, String> {
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
            .map_err(|e| format!("WS send failed: {}", e))?;
        rx.await.map_err(|_| "Request cancelled".to_string())
    }

    /// Subscribe to event notifications from the Python backend.
    pub fn subscribe(&self) -> broadcast::Receiver<EventNotification> {
        self.events.subscribe()
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
