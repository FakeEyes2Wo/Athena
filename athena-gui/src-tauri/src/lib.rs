mod python;
mod commands;
mod events;

use python::bridge::PythonBridge;
use std::sync::Arc;
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let resource_dir = app.path().resource_dir().ok();
            let bridge = tauri::async_runtime::block_on(async {
                PythonBridge::start(resource_dir)
                    .await
                    .expect("Failed to start Python backend")
            });
            let bridge = Arc::new(bridge);
            events::start_event_relay(app.handle().clone(), bridge.clone());
            app.manage(bridge);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::chat::send_message,
            commands::chat::message,
            commands::search::start_search,
            commands::search::pause_search,
            commands::search::resume_search,
            commands::search::stop_search,
            commands::validate::start_validation,
            commands::validate::generate_report,
            commands::research::tree_get,
            commands::research::tree_save,
            commands::research::tree_load,
            commands::research::eda_report,
            commands::settings::settings_get,
            commands::settings::settings_set,
            commands::settings::set_project_root,
            commands::traces::traces_list,
            commands::traces::trace_get,
            commands::graph::hypothesis_graph,
            commands::graph::graph_algorithms,
            commands::graph::graph_algorithm,
            commands::experiments::experiments_list,
            commands::experiments::experiment_get,
            commands::experiments::experiment_transition,
            commands::experiments::experiment_set_sota,
            commands::state::state_get,
            commands::state::sessions_list,
            commands::state::session_switch,
            commands::state::session_delete,
            commands::state::human_pending,
            commands::state::human_reply,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
