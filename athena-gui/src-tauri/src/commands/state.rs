use crate::python::bridge::PythonBridge;
use serde_json::json;
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub async fn state_get(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("state_get", json!({})).await
}

#[tauri::command]
pub async fn sessions_list(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("sessions_list", json!({})).await
}

#[tauri::command]
pub async fn sessions_list_for(
    path: String,
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state
        .call("sessions_list_for", json!({ "path": path }))
        .await
}

#[tauri::command]
pub async fn session_switch(
    session_id: String,
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state
        .call("session_switch", json!({ "session_id": session_id }))
        .await
}

#[tauri::command]
pub async fn session_delete(
    session_id: String,
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state
        .call("session_delete", json!({ "session_id": session_id }))
        .await
}

#[tauri::command]
pub async fn human_pending(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("human_pending", json!({})).await
}
