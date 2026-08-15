use crate::python::bridge::PythonBridge;
use serde_json::json;
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub async fn experiments_list(
    state: State<'_, Arc<PythonBridge>>,
    kind: Option<String>,
) -> Result<serde_json::Value, String> {
    state.call("experiments_list", json!({"kind": kind})).await
}

#[tauri::command]
pub async fn experiment_get(
    state: State<'_, Arc<PythonBridge>>,
    experiment_id: String,
) -> Result<serde_json::Value, String> {
    state
        .call("experiment_get", json!({"experiment_id": experiment_id}))
        .await
}

#[tauri::command]
pub async fn experiment_transition(
    state: State<'_, Arc<PythonBridge>>,
    experiment_id: String,
    status: String,
    error: Option<String>,
) -> Result<serde_json::Value, String> {
    state
        .call(
            "experiment_transition",
            json!({"experiment_id": experiment_id, "status": status, "error": error}),
        )
        .await
}

#[tauri::command]
pub async fn experiment_set_sota(
    state: State<'_, Arc<PythonBridge>>,
    experiment_id: String,
) -> Result<serde_json::Value, String> {
    state
        .call("experiment_set_sota", json!({"experiment_id": experiment_id}))
        .await
}
