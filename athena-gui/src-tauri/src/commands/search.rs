use crate::python::bridge::PythonBridge;
use serde_json::json;
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub async fn start_search(
    draft_id: String,
    revision: u64,
    acknowledge_unresolved: bool,
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state
        .call(
            "start_search",
            json!({
                "draft_id": draft_id,
                "revision": revision,
                "acknowledge_unresolved": acknowledge_unresolved,
            }),
        )
        .await
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
pub async fn stop_search(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("stop", json!({})).await
}
