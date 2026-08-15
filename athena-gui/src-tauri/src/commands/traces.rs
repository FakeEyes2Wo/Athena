use crate::python::bridge::PythonBridge;
use serde_json::json;
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub async fn traces_list(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("traces_list", json!({})).await
}

#[tauri::command]
pub async fn trace_get(
    state: State<'_, Arc<PythonBridge>>,
    agent_id: String,
) -> Result<serde_json::Value, String> {
    state.call("trace_get", json!({"agent_id": agent_id})).await
}
