"""Freshness guard for declared experiment outputs.

Prevents a silent failure mode where a command exits successfully but writes
nothing, and the framework scores stale files left in the output directory from
the parent commit. The guard archives old outputs by version before the run;
afterwards any file present in a declared output directory must have been
produced by this run.

A declared output may be a **directory** (``"predictions": "predictions"``) or a
**file** (``"report": "REPORT.md"``) — both shapes occur in real manifests, so
archiving must not change which one a path is. See ``archive_output_roots``.
"""

import shutil
from collections.abc import Mapping
from pathlib import Path

from athena.core.workspace import GitWorkspaceError, resolve_workspace_path


class OutputFreshnessError(RuntimeError):
    """Raised when a required declared output produced no new artifact."""

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path
        super().__init__(
            f"{name} output produced no new artifact: {path}. "
            "The command exited successfully but did not write any file; "
            "the previous version was archived beside the workspace under "
            "<workspace>-output-history."
        )


def _valid_version(version: str) -> None:
    """Reject path traversal in the version segment."""
    if not version or Path(version).name != version or version in {".", ".."}:
        raise ValueError("version must be a single path segment")


def _history_root(workdir: Path, version: str) -> Path:
    _valid_version(version)
    # Keep the archive outside the Git worktree so it never enters experiment diffs.
    return workdir.parent / f"{workdir.name}-output-history" / version


def _resolve(workdir: Path, rel: str) -> Path:
    """Resolve a declared output path, refusing to leave the workspace.

    ``resolve_workspace_path`` signals an escape with ``ValueError``; the guard
    reports it as ``GitWorkspaceError`` because from the caller's side this is a
    workspace-integrity failure, not a bad argument.
    """
    try:
        return resolve_workspace_path(workdir, rel)
    except ValueError as exc:
        raise GitWorkspaceError(str(exc)) from exc


def archive_output_roots(
    workdir: Path,
    outputs: Mapping[str, str],
    *,
    version: str,
) -> None:
    """Move existing declared outputs into a versioned history, then clear them.

    ``version`` should uniquely identify this run/turn (for example
    ``f"{plan_id}-{turn}"``). Old outputs are not deleted: they remain under
    ``<workdir>-output-history/<version>/`` until explicitly cleaned.

    **A path is only recreated as an empty directory if it already was one.**
    Recreating unconditionally turned a file output into a directory, and the
    command's next write to it failed with ``PermissionError`` — a manifest
    declaring ``"report": "REPORT.md"`` could not produce its report at all.
    Paths that were files, or did not exist, are left absent: the command
    creates them, and ``assert_output_roots`` fails honestly if it does not.
    """
    history = _history_root(workdir, version)
    for rel in outputs.values():
        root = _resolve(workdir, rel)
        was_dir = root.is_dir()
        if root.exists():
            target = history / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(root), str(target))
        # The command needs somewhere to write, but only the container.
        root.parent.mkdir(parents=True, exist_ok=True)
        if was_dir:
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
        root = _resolve(workdir, rel)
        saved = history / rel
        if saved.exists():
            if root.exists():
                if root.is_dir():
                    shutil.rmtree(root)
                else:
                    root.unlink()
            root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(saved), str(root))


def assert_output_roots(
    workdir: Path,
    outputs: Mapping[str, str],
    *,
    required: set[str],
) -> None:
    """Assert every required declared output carries something this run wrote.

    Satisfied by a non-empty directory or a non-empty file, because a manifest
    may declare either shape.
    """
    for name, rel in outputs.items():
        if name not in required:
            continue
        root = _resolve(workdir, rel)
        if root.is_dir():
            if not any(root.iterdir()):
                raise OutputFreshnessError(name, root)
        elif root.is_file():
            if root.stat().st_size == 0:
                raise OutputFreshnessError(name, root)
        else:
            raise OutputFreshnessError(name, root)


__all__ = [
    "OutputFreshnessError",
    "archive_output_roots",
    "assert_output_roots",
    "restore_output_roots",
]
