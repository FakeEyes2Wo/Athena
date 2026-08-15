use crate::python::bridge::PythonBridge;
use serde_json::json;
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub async fn hypothesis_graph(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("hypothesis_graph", json!({})).await
}

#[tauri::command]
pub async fn graph_algorithms(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("graph_algorithms", json!({})).await
}

#[tauri::command]
pub async fn graph_algorithm(
    state: State<'_, Arc<PythonBridge>>,
    name: String,
    params: serde_json::Value,
) -> Result<serde_json::Value, String> {
    state.call("graph_algorithm", json!({"name": name, "params": params})).await
}
