use crate::python::bridge::PythonBridge;
use serde_json::json;
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub async fn settings_get(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("settings_get", json!({})).await
}

#[tauri::command]
pub async fn settings_set(
    state: State<'_, Arc<PythonBridge>>,
    patch: serde_json::Value,
) -> Result<serde_json::Value, String> {
    state.call("settings_set", json!({"patch": patch})).await
}

#[tauri::command]
pub async fn set_project_root(
    state: State<'_, Arc<PythonBridge>>,
    path: String,
) -> Result<serde_json::Value, String> {
    state.call("set_project_root", json!({"path": path})).await
}
