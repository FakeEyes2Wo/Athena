//! Parity tests for `LocalGitWorkspace` on real temporary repositories,
//! mirroring `test/unit/test_git_workspace.py`.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use athena_types::ArtifactRef;
use athena_workspace::{
    DiffWriter, GitRunner, GitWorkBranch, GitWorkspaceError, LocalGitWorkspace, SystemGit,
};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use tokio::sync::Mutex;

/// Records each written diff so tests can assert on its bytes, returning a fresh
/// artifact ref per call.
struct RecordingDiffWriter {
    artifacts: Mutex<HashMap<String, Vec<u8>>>,
    counter: AtomicUsize,
}

impl RecordingDiffWriter {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            artifacts: Mutex::new(HashMap::new()),
            counter: AtomicUsize::new(0),
        })
    }

    async fn get(&self, key: &ArtifactRef) -> Vec<u8> {
        self.artifacts
            .lock()
            .await
            .get(key.as_str())
            .cloned()
            .expect("artifact recorded")
    }
}

#[async_trait::async_trait]
impl DiffWriter for RecordingDiffWriter {
    async fn write(&self, diff: Vec<u8>) -> Result<ArtifactRef, GitWorkspaceError> {
        let n = self.counter.fetch_add(1, Ordering::SeqCst);
        let reference = format!("artifact://git-diff/{n}");
        self.artifacts.lock().await.insert(reference.clone(), diff);
        Ok(ArtifactRef::new(reference).unwrap())
    }
}

/// Wraps a runner and fails the first `git branch -D` to exercise retryability.
struct FlakyBranchDelete {
    inner: Arc<dyn GitRunner>,
    failed: AtomicBool,
}

#[async_trait::async_trait]
impl GitRunner for FlakyBranchDelete {
    async fn run(
        &self,
        args: &[&str],
        cwd: &Path,
        check: bool,
    ) -> Result<Vec<u8>, GitWorkspaceError> {
        if args.first() == Some(&"branch")
            && args.get(1) == Some(&"-D")
            && !self.failed.swap(true, Ordering::SeqCst)
        {
            return Err(GitWorkspaceError("injected branch deletion failure".into()));
        }
        self.inner.run(args, cwd, check).await
    }
}

fn git(repo: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .expect("git runs");
    assert!(
        out.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

/// Build a base repo with one commit and an `already-exists` branch. Returns
/// (tempdir, repo, worktree_root, base_commit).
fn base_repo() -> (tempfile::TempDir, PathBuf, PathBuf, String) {
    let temp = tempfile::tempdir().unwrap();
    let repo = temp.path().join("repo");
    let worktrees = temp.path().join("worktrees");
    std::fs::create_dir(&repo).unwrap();

    git(&repo, &["init", "-b", "main"]);
    git(&repo, &["config", "user.name", "Athena Test"]);
    git(&repo, &["config", "user.email", "athena@example.invalid"]);
    std::fs::write(repo.join("seed.txt"), "baseline\n").unwrap();
    git(&repo, &["add", "seed.txt"]);
    git(&repo, &["commit", "--no-gpg-sign", "-m", "baseline"]);
    let base = git(&repo, &["rev-parse", "HEAD"]);
    git(&repo, &["branch", "already-exists", &base]);
    (temp, repo, worktrees, base)
}

fn manager(repo: &Path, worktrees: &Path, writer: Arc<RecordingDiffWriter>) -> LocalGitWorkspace {
    LocalGitWorkspace::new(repo.to_path_buf(), worktrees.to_path_buf(), writer).unwrap()
}

#[tokio::test]
async fn diff_rejects_post_review_change_then_commits_new_review() {
    let (_temp, repo, worktrees, base) = base_repo();
    let writer = RecordingDiffWriter::new();
    let ws_mgr = manager(&repo, &worktrees, writer.clone());

    let workspace = ws_mgr.create(&base, "experiment/reviewed").await.unwrap();
    let path = PathBuf::from(&workspace.path);
    std::fs::write(path.join("new.txt"), "first version\n").unwrap();
    std::fs::write(
        path.join("weights.bin"),
        [bytes_0_255(), bytes_0_255()].concat(),
    )
    .unwrap();

    let first_ref = ws_mgr.diff(&workspace).await.unwrap();
    let first = writer.get(&first_ref).await;
    assert!(contains(&first, b"diff --git a/new.txt b/new.txt"));
    assert!(contains(&first, b"first version"));
    assert!(contains(&first, b"GIT binary patch"));

    // Any tracked change after diff must re-enter review.
    std::fs::write(path.join("new.txt"), "approved version\n").unwrap();
    assert!(
        ws_mgr
            .commit(&workspace, &first_ref, "stale review")
            .await
            .is_err()
    );

    let second_ref = ws_mgr.diff(&workspace).await.unwrap();
    assert_ne!(first_ref, second_ref);
    assert!(
        ws_mgr
            .commit(&workspace, &first_ref, "must bind approved ref")
            .await
            .is_err()
    );

    let surprise = path.join("not-reviewed.txt");
    std::fs::write(&surprise, "not reviewed\n").unwrap();
    assert!(
        ws_mgr
            .commit(&workspace, &second_ref, "untracked file")
            .await
            .is_err()
    );
    std::fs::remove_file(&surprise).unwrap();

    let commit = ws_mgr
        .commit(&workspace, &second_ref, "checkpoint approved diff")
        .await
        .unwrap();
    let saved = git(&path, &["show", &format!("{}:new.txt", commit.as_str())]);
    assert_eq!("approved version", saved);
    assert_eq!("", git(&path, &["status", "--porcelain"]));

    // Idempotent retry returns the same commit.
    let again = ws_mgr
        .commit(&workspace, &second_ref, "idempotent retry")
        .await
        .unwrap();
    assert_eq!(commit, again);

    ws_mgr.remove(&workspace, true, false).await.unwrap();
    assert!(!path.exists());
    let branch = Command::new("git")
        .args([
            "-C",
            repo.to_str().unwrap(),
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/experiment/reviewed",
        ])
        .status()
        .unwrap();
    assert_eq!(Some(1), branch.code());
}

#[tokio::test]
async fn empty_diff_reuses_head_without_creating_commit() {
    let (_temp, repo, worktrees, base) = base_repo();
    let writer = RecordingDiffWriter::new();
    let ws_mgr = manager(&repo, &worktrees, writer.clone());

    let workspace = ws_mgr
        .create(&base, "experiment/config-only")
        .await
        .unwrap();
    let diff_ref = ws_mgr.diff(&workspace).await.unwrap();
    assert_eq!(Vec::<u8>::new(), writer.get(&diff_ref).await);

    let committed = ws_mgr
        .commit(&workspace, &diff_ref, "config-only")
        .await
        .unwrap();
    assert_eq!(base, committed.as_str());
    assert_eq!(
        base,
        git(&PathBuf::from(&workspace.path), &["rev-parse", "HEAD"])
    );
    ws_mgr.remove(&workspace, true, false).await.unwrap();
}

#[tokio::test]
async fn dirty_remove_requires_explicit_force() {
    let (_temp, repo, worktrees, base) = base_repo();
    let ws_mgr = manager(&repo, &worktrees, RecordingDiffWriter::new());

    let workspace = ws_mgr.create(&base, "experiment/discard").await.unwrap();
    let path = PathBuf::from(&workspace.path);
    std::fs::write(path.join("dirty.txt"), "discard me\n").unwrap();

    assert!(ws_mgr.remove(&workspace, false, false).await.is_err());
    assert!(path.exists());

    ws_mgr.remove(&workspace, true, true).await.unwrap();
    assert!(!path.exists());
}

#[tokio::test]
async fn rejects_invalid_or_unowned_inputs() {
    let (temp, repo, worktrees, base) = base_repo();
    let ws_mgr = manager(&repo, &worktrees, RecordingDiffWriter::new());

    assert!(
        ws_mgr
            .create("not-a-hash", "experiment/invalid-commit")
            .await
            .is_err()
    );
    assert!(
        ws_mgr
            .create(&"f".repeat(40), "experiment/unknown-commit")
            .await
            .is_err()
    );
    assert!(ws_mgr.create(&base, "../invalid").await.is_err());
    assert!(ws_mgr.create(&base, "already-exists").await.is_err());

    let forged = GitWorkBranch {
        path: temp.path().join("outside").to_str().unwrap().to_string(),
        branch: "experiment/forged".into(),
        base_commit: athena_types::CommitHash::new(base.clone()).unwrap(),
    };
    assert!(ws_mgr.diff(&forged).await.is_err());

    let forged_inside = GitWorkBranch {
        path: worktrees
            .join("never-created")
            .to_str()
            .unwrap()
            .to_string(),
        ..forged.clone()
    };
    assert!(ws_mgr.diff(&forged_inside).await.is_err());

    let workspace = ws_mgr.create(&base, "experiment/no-review").await.unwrap();
    let missing = ArtifactRef::new("artifact://diff/missing").unwrap();
    assert!(
        ws_mgr
            .commit(&workspace, &missing, "not reviewed")
            .await
            .is_err()
    );
    ws_mgr.remove(&workspace, true, true).await.unwrap();
}

#[tokio::test]
async fn branch_delete_failure_can_be_retried() {
    let (_temp, repo, worktrees, base) = base_repo();
    let git_runner = Arc::new(FlakyBranchDelete {
        inner: Arc::new(SystemGit),
        failed: AtomicBool::new(false),
    });
    let ws_mgr = LocalGitWorkspace::with_runner(
        repo.clone(),
        worktrees.clone(),
        RecordingDiffWriter::new(),
        git_runner,
    )
    .unwrap();

    let workspace = ws_mgr
        .create(&base, "experiment/retry-remove")
        .await
        .unwrap();
    let path = PathBuf::from(&workspace.path);

    // First removal: worktree is removed, branch deletion fails → error, retained.
    assert!(ws_mgr.remove(&workspace, true, false).await.is_err());
    assert!(!path.exists());

    // Retry succeeds now that the injected failure is spent.
    ws_mgr.remove(&workspace, true, false).await.unwrap();
}

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

fn bytes_0_255() -> Vec<u8> {
    (0u16..256).map(|b| b as u8).collect()
}
