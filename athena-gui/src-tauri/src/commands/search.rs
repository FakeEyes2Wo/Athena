use tauri::State;
use std::sync::Arc;
use crate::python::bridge::PythonBridge;
use serde_json::json;

#[tauri::command]
pub async fn start_search(
    state: State<'_, Arc<PythonBridge>>,
    config: serde_json::Value,
) -> Result<serde_json::Value, String> {
    state.call("start_search", json!({"config": config})).await
}

#[tauri::command]
pub async fn pause_search(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("pause", json!({})).await
}

#[tauri::command]
pub async fn resume_search(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("resume", json!({})).await
}

#[tauri::command]
pub async fn stop_search(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("stop", json!({})).await
}
