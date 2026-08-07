use crate::python::bridge::PythonBridge;
use serde_json::json;
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub async fn tree_get(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("tree_get", json!({})).await
}

#[tauri::command]
pub async fn tree_save(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("tree_save", json!({})).await
}

#[tauri::command]
pub async fn tree_load(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("tree_load", json!({})).await
}
