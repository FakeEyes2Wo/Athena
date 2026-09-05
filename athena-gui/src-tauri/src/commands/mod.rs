mod clarification;
mod dialog;

pub use clarification::*;
pub use dialog::*;

use crate::python::PythonBridge;
use serde_json::json;
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub async fn message(
    state: State<'_, Arc<PythonBridge>>,
    text: String,
) -> Result<serde_json::Value, String> {
    state.call("message", json!({ "text": text })).await
}

#[tauri::command]
pub async fn experiments_list(
    state: State<'_, Arc<PythonBridge>>,
    kind: Option<String>,
) -> Result<serde_json::Value, String> {
    state
        .call("experiments_list", json!({ "kind": kind }))
        .await
}

#[tauri::command]
pub async fn experiment_get(
    state: State<'_, Arc<PythonBridge>>,
    experiment_id: String,
) -> Result<serde_json::Value, String> {
    state
        .call("experiment_get", json!({ "experiment_id": experiment_id }))
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
            json!({ "experiment_id": experiment_id, "status": status, "error": error }),
        )
        .await
}

#[tauri::command]
pub async fn experiment_set_sota(
    state: State<'_, Arc<PythonBridge>>,
    experiment_id: String,
) -> Result<serde_json::Value, String> {
    state
        .call(
            "experiment_set_sota",
            json!({ "experiment_id": experiment_id }),
        )
        .await
}

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
    state
        .call("graph_algorithm", json!({ "name": name, "params": params }))
        .await
}

#[tauri::command]
pub async fn tree_get(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("tree_get", json!({})).await
}

#[tauri::command]
pub async fn tree_save(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("tree_save", json!({})).await
}

#[tauri::command]
pub async fn tree_load(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("tree_load", json!({})).await
}

#[tauri::command]
pub async fn eda_report(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("eda_report", json!({})).await
}

#[tauri::command]
pub async fn start_search(
    draft_id: String,
    revision: u64,
    acknowledge_unresolved: bool,
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state
        .call(
            "start_search",
            json!({
                "draft_id": draft_id,
                "revision": revision,
                "acknowledge_unresolved": acknowledge_unresolved,
            }),
        )
        .await
}

#[tauri::command]
pub async fn pause_search(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("pause", json!({})).await
}

#[tauri::command]
pub async fn resume_search(
    state: State<'_, Arc<PythonBridge>>,
) -> Result<serde_json::Value, String> {
    state.call("resume", json!({})).await
}

#[tauri::command]
pub async fn stop_search(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("stop", json!({})).await
}

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
    state.call("settings_set", json!({ "patch": patch })).await
}

#[tauri::command]
pub async fn set_project_root(
    state: State<'_, Arc<PythonBridge>>,
    path: String,
) -> Result<serde_json::Value, String> {
    state
        .call("set_project_root", json!({ "path": path }))
        .await
}

#[tauri::command]
pub async fn traces_list(state: State<'_, Arc<PythonBridge>>) -> Result<serde_json::Value, String> {
    state.call("traces_list", json!({})).await
}

#[tauri::command]
pub async fn trace_get(
    state: State<'_, Arc<PythonBridge>>,
    agent_id: String,
) -> Result<serde_json::Value, String> {
    state
        .call("trace_get", json!({ "agent_id": agent_id }))
        .await
}

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
