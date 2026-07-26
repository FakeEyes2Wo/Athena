use crate::error::GitWorkspaceError;
use std::path::Path;
use std::process::Stdio;
use tokio::process::Command;

/// Runs `git` subprocesses. Injectable so tests can simulate transient failures
/// (mirrors the Python test that monkeypatches `_git`).
#[async_trait::async_trait]
pub trait GitRunner: Send + Sync {
    /// Run `git -C <cwd> <args...>`, returning stdout. When `check` is true a
    /// non-zero exit status becomes a [`GitWorkspaceError`].
    async fn run(
        &self,
        args: &[&str],
        cwd: &Path,
        check: bool,
    ) -> Result<Vec<u8>, GitWorkspaceError>;
}

/// Real `git` runner.
///
/// Always uses an argv array (never a shell string, so there is no injection
/// surface) and pins a fixed, hermetic committer identity so machine-generated
/// experiment commits succeed regardless of the ambient git configuration.
pub struct SystemGit;

#[async_trait::async_trait]
impl GitRunner for SystemGit {
    async fn run(
        &self,
        args: &[&str],
        cwd: &Path,
        check: bool,
    ) -> Result<Vec<u8>, GitWorkspaceError> {
        let mut cmd = Command::new("git");
        cmd.arg("-C")
            .arg(cwd)
            .args(args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env("GIT_AUTHOR_NAME", "Athena")
            .env("GIT_AUTHOR_EMAIL", "athena@example.invalid")
            .env("GIT_COMMITTER_NAME", "Athena")
            .env("GIT_COMMITTER_EMAIL", "athena@example.invalid");

        let output = cmd
            .output()
            .await
            .map_err(|e| GitWorkspaceError::new(format!("failed to spawn git: {e}")))?;

        if check && !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            let snippet: String = stderr.chars().take(200).collect();
            return Err(GitWorkspaceError::new(format!(
                "git {} failed: {snippet}",
                args.join(" ")
            )));
        }
        Ok(output.stdout)
    }
}
