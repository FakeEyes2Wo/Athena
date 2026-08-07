//! `athena-workspace` — local Git worktree, diff, and commit operations for
//! experiment code snapshots.
//!
//! The manager owns only the Git boundary: create an isolated worktree, freeze a
//! reviewable binary diff, commit exactly the reviewed content, and explicitly
//! reclaim the directory. Every git call uses an argv array (no shell), and
//! removals are confined to worktrees this manager created.

mod command;
mod error;
mod local;
mod model;

pub use command::{GitRunner, SystemGit};
pub use error::GitWorkspaceError;
pub use local::{DiffWriter, LocalGitWorkspace};
pub use model::GitWorkBranch;
