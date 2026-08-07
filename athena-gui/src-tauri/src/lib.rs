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
            let bridge = tauri::async_runtime::block_on(async {
                PythonBridge::start()
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
            commands::search::start_search,
            commands::search::pause_search,
            commands::search::resume_search,
            commands::search::stop_search,
            commands::validate::start_validation,
            commands::validate::generate_report,
            commands::research::tree_get,
            commands::research::tree_save,
            commands::research::tree_load,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
