use crate::command::{GitRunner, SystemGit};
use crate::error::GitWorkspaceError;
use crate::model::GitWorkBranch;
use athena_types::{ArtifactRef, CommitHash};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::sync::Mutex;
use uuid::Uuid;

/// Persists a staged binary diff and returns a stable artifact reference.
///
/// This is the only seam to the artifact store; the workspace never copies the
/// repository or invents its own snapshot format.
#[async_trait::async_trait]
pub trait DiffWriter: Send + Sync {
    async fn write(&self, diff: Vec<u8>) -> Result<ArtifactRef, GitWorkspaceError>;
}

/// Frozen state of the most recent `diff`, bound to a single `commit`.
#[derive(Clone)]
struct Review {
    artifact: ArtifactRef,
    /// Raw staged bytes captured at review time; equality detects tampering.
    staged: Vec<u8>,
    tree: String,
    head: String,
    empty: bool,
}

struct WorktreeState {
    review: Option<Review>,
    committed: Option<String>,
}

struct Inner {
    repo: PathBuf,
    states: HashMap<PathBuf, WorktreeState>,
}

/// Minimal Git worktree manager: atomic create, diff, commit, remove.
///
/// One async lock guards the mutable registry for the duration of each
/// operation, matching the current Python `LocalGitWorkspace`.
pub struct LocalGitWorkspace {
    root: PathBuf,
    git: Arc<dyn GitRunner>,
    diff_writer: Arc<dyn DiffWriter>,
    inner: Mutex<Inner>,
}

impl LocalGitWorkspace {
    /// Create a manager rooted at `repo_path`, placing worktrees under
    /// `worktree_root` and writing diffs through `diff_writer`.
    pub fn new(
        repo_path: PathBuf,
        worktree_root: PathBuf,
        diff_writer: Arc<dyn DiffWriter>,
    ) -> Result<Self, GitWorkspaceError> {
        Self::with_runner(repo_path, worktree_root, diff_writer, Arc::new(SystemGit))
    }

    /// Like [`new`](Self::new) but with an injected [`GitRunner`] (for tests).
    pub fn with_runner(
        repo_path: PathBuf,
        worktree_root: PathBuf,
        diff_writer: Arc<dyn DiffWriter>,
        git: Arc<dyn GitRunner>,
    ) -> Result<Self, GitWorkspaceError> {
        std::fs::create_dir_all(&worktree_root)
            .map_err(|e| GitWorkspaceError::new(format!("cannot create worktree root: {e}")))?;
        let repo = abs(&repo_path)?;
        let root = abs(&worktree_root)?;
        Ok(Self {
            root,
            git,
            diff_writer,
            inner: Mutex::new(Inner {
                repo,
                states: HashMap::new(),
            }),
        })
    }

    /// Initialize a fresh repository with a single initial commit and return its
    /// hash. Idempotent: re-initializing an existing repo returns current HEAD.
    pub async fn init(
        &self,
        initial_file: &str,
        initial_content: &str,
    ) -> Result<CommitHash, GitWorkspaceError> {
        let mut inner = self.inner.lock().await;
        let repo = inner.repo.clone();

        // Already a repository → return HEAD instead of re-committing.
        if self
            .git
            .run(&["rev-parse", "HEAD"], &repo, false)
            .await
            .map(|o| !o.is_empty())
            .unwrap_or(false)
        {
            let head = self.git.run(&["rev-parse", "HEAD"], &repo, true).await?;
            return commit_hash(&head);
        }

        std::fs::create_dir_all(&repo)
            .map_err(|e| GitWorkspaceError::new(format!("cannot create repo dir: {e}")))?;
        self.git.run(&["init", "-b", "main"], &repo, true).await?;
        let file = repo.join(initial_file);
        std::fs::write(&file, initial_content)
            .map_err(|e| GitWorkspaceError::new(format!("cannot write initial file: {e}")))?;
        self.git.run(&["add", initial_file], &repo, true).await?;
        self.git
            .run(&["commit", "-m", "initial commit"], &repo, true)
            .await?;
        let head = self.git.run(&["rev-parse", "HEAD"], &repo, true).await?;
        inner.repo = repo;
        commit_hash(&head)
    }

    /// Create a unique temporary branch and isolated worktree from `base_commit`.
    pub async fn create(
        &self,
        base_commit: &str,
        branch: &str,
    ) -> Result<GitWorkBranch, GitWorkspaceError> {
        let mut inner = self.inner.lock().await;
        let repo = inner.repo.clone();

        let commit = self.resolve_commit(&repo, base_commit).await?;
        validate_branch(branch)?;

        let branch_ref = format!("refs/heads/{branch}");
        let existing = self
            .git
            .run(&["show-ref", "--verify", &branch_ref], &repo, false)
            .await?;
        if !trimmed(&existing).is_empty() {
            return Err(GitWorkspaceError::new(format!(
                "branch already exists: {branch}"
            )));
        }

        let path = self
            .root
            .join(format!("athena-{}", Uuid::new_v4().simple()));
        let path_str = path_to_str(&path)?;

        // update-ref with a 40-zero old value creates the ref only if absent.
        self.git
            .run(
                &["update-ref", &branch_ref, &commit, &"0".repeat(40)],
                &repo,
                true,
            )
            .await?;
        if self
            .git
            .run(&["worktree", "add", &path_str, branch], &repo, true)
            .await
            .is_err()
        {
            let _ = self.git.run(&["branch", "-D", branch], &repo, false).await;
            return Err(GitWorkspaceError::new("branch creation failed"));
        }

        let ws = GitWorkBranch {
            path: path_str,
            branch: branch.to_string(),
            base_commit: CommitHash::new(commit)
                .map_err(|e| GitWorkspaceError::new(e.to_string()))?,
        };
        inner.states.insert(
            abs(&path)?,
            WorktreeState {
                review: None,
                committed: None,
            },
        );
        Ok(ws)
    }

    /// Stage every change and freeze a binary diff relative to the base commit.
    pub async fn diff(&self, workspace: &GitWorkBranch) -> Result<ArtifactRef, GitWorkspaceError> {
        let mut inner = self.inner.lock().await;
        let key = self.state_key(&inner, workspace)?;
        if inner.states[&key].committed.is_some() {
            return Err(GitWorkspaceError::new("already committed; cannot diff"));
        }
        let path = PathBuf::from(&workspace.path);
        let base = workspace.base_commit.as_str().to_string();

        self.git.run(&["add", "-A"], &path, true).await?;
        let staged = self
            .git
            .run(&["diff", "--cached", "--binary", &base], &path, true)
            .await?;
        let unstaged = self.git.run(&["diff"], &path, true).await?;
        let others = self.git.run(&["ls-files", "--others"], &path, true).await?;
        if !unstaged.is_empty() || !others.is_empty() {
            return Err(GitWorkspaceError::new(
                "worktree still has unstaged changes",
            ));
        }

        let artifact = self.diff_writer.write(staged.clone()).await?;
        let tree = trimmed(&self.git.run(&["write-tree"], &path, true).await?);
        let head = trimmed(&self.git.run(&["rev-parse", "HEAD"], &path, true).await?);

        if let Some(state) = inner.states.get_mut(&key) {
            state.review = Some(Review {
                artifact: artifact.clone(),
                empty: staged.is_empty(),
                staged,
                tree,
                head,
            });
        }
        Ok(artifact)
    }

    /// Commit exactly the content reviewed by the most recent `diff`, unchanged
    /// since. An empty diff reuses HEAD without creating a commit.
    pub async fn commit(
        &self,
        workspace: &GitWorkBranch,
        approved_artifact: &ArtifactRef,
        message: &str,
    ) -> Result<CommitHash, GitWorkspaceError> {
        let mut inner = self.inner.lock().await;
        let key = self.state_key(&inner, workspace)?;

        let review = match &inner.states[&key].review {
            Some(r) if &r.artifact == approved_artifact => r.clone(),
            _ => {
                return Err(GitWorkspaceError::new(
                    "approved artifact does not match the current diff",
                ));
            }
        };
        if let Some(existing) = &inner.states[&key].committed {
            return commit_hash(existing.as_bytes());
        }

        let path = PathBuf::from(&workspace.path);
        let base = workspace.base_commit.as_str().to_string();
        let current_head = trimmed(&self.git.run(&["rev-parse", "HEAD"], &path, true).await?);
        let current_tree = trimmed(&self.git.run(&["write-tree"], &path, true).await?);
        let staged = self
            .git
            .run(&["diff", "--cached", "--binary", &base], &path, true)
            .await?;
        let unstaged = self.git.run(&["diff"], &path, true).await?;
        let untracked = self
            .git
            .run(&["ls-files", "--others", "--exclude-standard"], &path, true)
            .await?;

        if staged != review.staged
            || current_head != review.head
            || current_tree != review.tree
            || !unstaged.is_empty()
            || !untracked.is_empty()
        {
            return Err(GitWorkspaceError::new("worktree changed after review"));
        }

        let committed = if review.empty {
            current_head.clone()
        } else {
            let new_commit = trimmed(
                &self
                    .git
                    .run(
                        &[
                            "commit-tree",
                            &current_tree,
                            "-p",
                            &current_head,
                            "-m",
                            message,
                        ],
                        &path,
                        true,
                    )
                    .await?,
            );
            self.git
                .run(
                    &[
                        "update-ref",
                        &format!("refs/heads/{}", workspace.branch),
                        &new_commit,
                        &current_head,
                    ],
                    &path,
                    true,
                )
                .await?;
            new_commit
        };

        if let Some(state) = inner.states.get_mut(&key) {
            state.committed = Some(committed.clone());
        }
        commit_hash(committed.as_bytes())
    }

    /// Remove the worktree; a dirty tree or branch deletion requires explicit
    /// authorization. A failed branch delete leaves the worktree registered so
    /// the caller can retry.
    pub async fn remove(
        &self,
        workspace: &GitWorkBranch,
        delete_branch: bool,
        force: bool,
    ) -> Result<(), GitWorkspaceError> {
        let mut inner = self.inner.lock().await;
        let key = self.state_key(&inner, workspace)?;
        let repo = inner.repo.clone();
        let path = PathBuf::from(&workspace.path);

        if path.exists() {
            if !force {
                let porcelain = self
                    .git
                    .run(&["status", "--porcelain"], &path, true)
                    .await?;
                if !porcelain.is_empty() {
                    return Err(GitWorkspaceError::new("worktree has uncommitted changes"));
                }
            }
            let path_str = path_to_str(&path)?;
            let mut args = vec!["worktree", "remove"];
            if force {
                args.push("--force");
            }
            args.push(&path_str);
            self.git.run(&args, &repo, true).await?;
        }
        if delete_branch {
            // If this fails we return before de-registering, so the caller can retry.
            self.git
                .run(&["branch", "-D", &workspace.branch], &repo, true)
                .await?;
        }
        inner.states.remove(&key);
        Ok(())
    }

    // ── internals ──

    async fn resolve_commit(&self, repo: &Path, commit: &str) -> Result<String, GitWorkspaceError> {
        let commit = commit.trim();
        if commit.is_empty() {
            return Err(GitWorkspaceError::new("commit cannot be empty"));
        }
        let spec = format!("{commit}^{{commit}}");
        match self
            .git
            .run(&["rev-parse", "--verify", &spec], repo, true)
            .await
        {
            Ok(out) => Ok(trimmed(&out)),
            Err(_) => Err(GitWorkspaceError::new(format!(
                "unknown or non-commit object: {commit}"
            ))),
        }
    }

    /// Resolve a workspace to its registry key, rejecting unknown/forged paths.
    fn state_key(&self, inner: &Inner, ws: &GitWorkBranch) -> Result<PathBuf, GitWorkspaceError> {
        let key = abs(Path::new(&ws.path))?;
        if inner.states.contains_key(&key) {
            Ok(key)
        } else {
            Err(GitWorkspaceError::new("unknown worktree"))
        }
    }
}

/// Lexical absolute normalization — independent of whether the path exists, so
/// registry lookups still work after a worktree directory is removed.
fn abs(path: &Path) -> Result<PathBuf, GitWorkspaceError> {
    std::path::absolute(path)
        .map_err(|e| GitWorkspaceError::new(format!("cannot normalize path: {e}")))
}

fn path_to_str(path: &Path) -> Result<String, GitWorkspaceError> {
    path.to_str()
        .map(str::to_string)
        .ok_or_else(|| GitWorkspaceError::new("path is not valid UTF-8"))
}

fn trimmed(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).trim().to_string()
}

fn commit_hash(bytes: &[u8]) -> Result<CommitHash, GitWorkspaceError> {
    CommitHash::new(trimmed(bytes)).map_err(|e| GitWorkspaceError::new(e.to_string()))
}

/// Reject branch names that are unsafe or that git would reinterpret.
fn validate_branch(branch: &str) -> Result<(), GitWorkspaceError> {
    let bad_start = branch.starts_with(['-', '/', '.']);
    let bad_end = branch.ends_with('/') || branch.ends_with('.') || branch.ends_with(".lock");
    let bad_char = branch
        .chars()
        .any(|c| c.is_whitespace() || matches!(c, '~' | '^' | ':' | '?' | '*' | '[' | '\\'));
    if branch.is_empty()
        || bad_start
        || bad_end
        || branch.contains("..")
        || branch.contains("@{")
        || bad_char
    {
        return Err(GitWorkspaceError::new("invalid branch name"));
    }
    Ok(())
}
