use tauri::State;
use std::sync::Arc;
use crate::python::bridge::PythonBridge;
use serde_json::json;

#[tauri::command]
pub async fn message(
    state: State<'_, Arc<PythonBridge>>,
    text: String,
) -> Result<serde_json::Value, String> {
    state.call("message", json!({"text": text})).await
}
