use tauri::State;
use std::sync::Arc;
use crate::python::bridge::PythonBridge;
use serde_json::json;

#[tauri::command]
pub async fn send_message(
    state: State<'_, Arc<PythonBridge>>,
    message: String,
) -> Result<serde_json::Value, String> {
    state.call("parse_intent", json!({"message": message})).await
}

#[tauri::command]
pub async fn message(
    state: State<'_, Arc<PythonBridge>>,
    text: String,
) -> Result<serde_json::Value, String> {
    state.call("message", json!({"text": text})).await
}
