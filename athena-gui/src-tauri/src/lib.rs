mod commands;
mod python;

use python::PythonBridge;
use std::sync::Arc;
use tauri::{AppHandle, Emitter, Manager};

fn start_event_relay(app: AppHandle, bridge: Arc<PythonBridge>) {
    let mut events = bridge.subscribe();
    tauri::async_runtime::spawn(async move {
        loop {
            match events.recv().await {
                Ok(event) => {
                    let kind = event.kind.clone();
                    let _ = app.emit(
                        &kind,
                        serde_json::json!({ "kind": event.kind, "data": event.data }),
                    );
                }
                Err(tokio::sync::broadcast::error::RecvError::Lagged(count)) => {
                    eprintln!("[athena-gui] event relay skipped {count} messages");
                }
                Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
            }
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let resource_dir = app.path().resource_dir().ok();
            let bridge = tauri::async_runtime::block_on(async {
                PythonBridge::start(resource_dir)
                    .await
                    .expect("Failed to start Python backend")
            });
            let bridge = Arc::new(bridge);
            start_event_relay(app.handle().clone(), bridge.clone());
            app.manage(bridge);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::message,
            commands::task_clarification_start,
            commands::task_clarification_get,
            commands::task_clarification_retry,
            commands::task_clarification_revise,
            commands::task_clarification_cancel,
            commands::workspace_dialog_start_directory,
            commands::start_search,
            commands::pause_search,
            commands::resume_search,
            commands::stop_search,
            commands::start_validation,
            commands::generate_report,
            commands::tree_get,
            commands::tree_save,
            commands::tree_load,
            commands::eda_report,
            commands::settings_get,
            commands::settings_set,
            commands::set_project_root,
            commands::traces_list,
            commands::trace_get,
            commands::hypothesis_graph,
            commands::graph_algorithms,
            commands::graph_algorithm,
            commands::experiments_list,
            commands::experiment_get,
            commands::experiment_transition,
            commands::experiment_set_sota,
            commands::state_get,
            commands::sessions_list,
            commands::sessions_list_for,
            commands::session_switch,
            commands::session_delete,
            commands::human_pending,
            commands::human_reply,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
