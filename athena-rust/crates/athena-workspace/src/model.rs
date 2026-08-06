use athena_types::CommitHash;
use serde::{Deserialize, Serialize};

/// A created experiment worktree: its path, unique branch, and base commit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GitWorkBranch {
    pub path: String,
    pub branch: String,
    pub base_commit: CommitHash,
}
