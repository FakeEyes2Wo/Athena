use tauri::State;
use std::sync::Arc;
use crate::python::bridge::PythonBridge;
use serde_json::json;

#[tauri::command]
pub async fn send_message(
    state: State<'_, Arc<PythonBridge>>,
    message: String,
) -> Result<serde_json::Value, String> {
    state.call("PARSE_INTENT", json!({"message": message})).await
}
