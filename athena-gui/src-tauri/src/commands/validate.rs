use tauri::State;
use std::sync::Arc;
use crate::python::bridge::PythonBridge;
use serde_json::json;

#[tauri::command]
pub async fn start_validation(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("start_validation", json!({})).await
}

#[tauri::command]
pub async fn generate_report(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("generate_report", json!({})).await
}
