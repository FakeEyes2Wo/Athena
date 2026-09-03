use std::path::PathBuf;
use tauri::{AppHandle, Manager};

fn first_existing_directory(candidates: impl IntoIterator<Item = PathBuf>) -> Option<PathBuf> {
    candidates.into_iter().find(|path| path.is_dir())
}

#[tauri::command]
pub fn workspace_dialog_start_directory(
    app: AppHandle,
    requested: Option<String>,
) -> Option<String> {
    first_existing_directory(
        [
            requested.map(PathBuf::from),
            app.path().home_dir().ok(),
            std::env::current_dir().ok(),
        ]
        .into_iter()
        .flatten(),
    )
    .map(|path| path.to_string_lossy().into_owned())
}

#[cfg(test)]
mod tests {
    use super::first_existing_directory;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_SANDBOX: AtomicU64 = AtomicU64::new(0);

    struct Sandbox {
        root: PathBuf,
    }

    impl Sandbox {
        fn new() -> Self {
            let sequence = NEXT_SANDBOX.fetch_add(1, Ordering::Relaxed);
            let root = std::env::temp_dir().join(format!(
                "athena-workspace-dialog-{}-{sequence}",
                std::process::id()
            ));
            fs::create_dir_all(&root).expect("create dialog test sandbox");
            Self { root }
        }

        fn directory(&self, name: &str) -> PathBuf {
            let path = self.root.join(name);
            fs::create_dir_all(&path).expect("create test directory");
            path
        }

        fn path(&self, name: &str) -> PathBuf {
            self.root.join(name)
        }
    }

    impl Drop for Sandbox {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    fn resolve(candidates: &[&Path]) -> Option<PathBuf> {
        first_existing_directory(candidates.iter().map(|path| path.to_path_buf()))
    }

    #[test]
    fn workspace_dialog_prefers_the_requested_directory() {
        let sandbox = Sandbox::new();
        let requested = sandbox.directory("requested");
        let home = sandbox.directory("home");
        let current = sandbox.directory("current");

        assert_eq!(resolve(&[&requested, &home, &current]), Some(requested));
    }

    #[test]
    fn workspace_dialog_skips_missing_paths_and_regular_files() {
        let sandbox = Sandbox::new();
        let missing = sandbox.path("missing");
        let file = sandbox.path("not-a-directory");
        fs::write(&file, "file").expect("create regular file");
        let home = sandbox.directory("home");

        assert_eq!(resolve(&[&missing, &file, &home]), Some(home));
    }

    #[test]
    fn workspace_dialog_falls_back_to_the_current_directory() {
        let sandbox = Sandbox::new();
        let missing_requested = sandbox.path("missing-requested");
        let missing_home = sandbox.path("missing-home");
        let current = sandbox.directory("current");

        assert_eq!(
            resolve(&[&missing_requested, &missing_home, &current]),
            Some(current)
        );
    }
}
