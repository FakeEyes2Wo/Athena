use tauri::{AppHandle, Emitter};
use std::sync::Arc;
use crate::python::bridge::PythonBridge;

/// Relay EventNotification messages from the Python backend to the Tauri frontend.
pub fn start_event_relay(app: AppHandle, bridge: Arc<PythonBridge>) {
    let mut rx = bridge.subscribe();
    tauri::async_runtime::spawn(async move {
        while let Ok(evt) = rx.recv().await {
            let _ = app.emit(
                &evt.kind,
                serde_json::json!({
                    "kind": evt.kind,
                    "data": evt.data,
                }),
            );
        }
    });
}
