"""Freshness guard for declared experiment outputs.

Prevents a silent failure mode where a command exits successfully but writes
nothing, and the framework scores stale files left in the output directory from
the parent commit. The guard archives old outputs by version before the run;
afterwards any file present in a declared output directory must have been
produced by this run.
"""

import shutil
from collections.abc import Mapping
from pathlib import Path

from athena.core.workspace import resolve_workspace_path


class OutputFreshnessError(RuntimeError):
    """Raised when a required declared output produced no new artifact."""

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path
        super().__init__(
            f"{name} output produced no new artifact: {path}. "
            "The command exited successfully but did not write any file; "
            "the previous version was archived under .output-history."
        )


def _valid_version(version: str) -> None:
    """Reject path traversal in the version segment."""
    if not version or Path(version).name != version or version in {".", ".."}:
        raise ValueError("version must be a single path segment")


def _history_root(workdir: Path, version: str) -> Path:
    _valid_version(version)
    # Keep the archive outside the Git worktree so it never enters experiment diffs.
    return workdir.parent / f"{workdir.name}-output-history" / version


def archive_output_roots(
    workdir: Path,
    outputs: Mapping[str, str],
    *,
    version: str,
) -> None:
    """Move existing declared outputs into a versioned history, then recreate them empty.

    ``version`` should uniquely identify this run/turn (for example
    ``f"{plan_id}-{turn}"``). Old outputs are not deleted: they remain under
    ``<workdir>/.output-history/<version>/`` until explicitly cleaned.
    """
    history = _history_root(workdir, version)
    for rel in outputs.values():
        root = resolve_workspace_path(workdir, rel)
        if root.exists():
            target = history / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(root), str(target))
        root.mkdir(parents=True, exist_ok=True)


def restore_output_roots(
    workdir: Path,
    outputs: Mapping[str, str],
    *,
    version: str,
) -> None:
    """Restore one version's archived outputs to their original paths."""
    history = _history_root(workdir, version)
    for rel in outputs.values():
        root = resolve_workspace_path(workdir, rel)
        saved = history / rel
        if saved.exists():
            if root.exists():
                if root.is_dir():
                    shutil.rmtree(root)
                else:
                    root.unlink()
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(saved), str(root))


def assert_output_roots(
    workdir: Path,
    outputs: Mapping[str, str],
    *,
    required: set[str],
) -> None:
    """Assert every required declared output directory exists and is non-empty."""
    for name, rel in outputs.items():
        if name not in required:
            continue
        root = resolve_workspace_path(workdir, rel)
        if not root.is_dir() or not any(root.iterdir()):
            raise OutputFreshnessError(name, root)


__all__ = [
    "OutputFreshnessError",
    "archive_output_roots",
    "assert_output_roots",
    "restore_output_roots",
]
