use tauri::{AppHandle, Emitter};
use std::sync::Arc;
use crate::python::bridge::PythonBridge;

/// Relay EventNotification messages from the Python backend to the Tauri frontend.
pub fn start_event_relay(app: AppHandle, bridge: Arc<PythonBridge>) {
    let mut rx = bridge.subscribe();
    tauri::async_runtime::spawn(async move {
        eprintln!("[athena-gui] event relay started");
        loop {
            match rx.recv().await {
                Ok(evt) => {
                    // 诊断日志：确认后端事件确实到达 Rust 侧（查看 `tauri dev` 终端）。
                    eprintln!("[athena-gui] relay {} ({} bytes)", evt.kind, evt.data.to_string().len());
                    let _ = app.emit(
                        &evt.kind,
                        serde_json::json!({
                            "kind": evt.kind,
                            "data": evt.data,
                        }),
                    );
                }
                Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                    // 慢于发送端时跳过积压消息，避免 while-let 模式在 Lagged 时永久退出。
                    eprintln!("[athena-gui] relay lagged, skipped {n} messages");
                }
                Err(tokio::sync::broadcast::error::RecvError::Closed) => {
                    eprintln!("[athena-gui] relay channel closed");
                    break;
                }
            }
        }
    });
}
