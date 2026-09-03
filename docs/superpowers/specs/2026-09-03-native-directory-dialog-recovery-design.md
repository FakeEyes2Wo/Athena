# Native Directory Dialog Recovery Design

Date: 2026-09-03
Status: approved for implementation

## Problem

Athena's Tauri directory picker passes the current workspace directly as
`defaultPath`. If that directory no longer exists, or if no workspace is available,
the Windows dialog may fall back to `%USERPROFILE%\Desktop`. GUI smoke runs use an
isolated temporary profile whose Desktop directory may not exist and whose profile is
removed after the run. Windows then shows a blocking “Location is not available”
warning instead of opening a usable directory picker.

The real Windows Desktop registry entry and the user's persisted Athena workspaces
remain valid. The failure is therefore an unchecked dialog start path, not corruption
of the user's Windows profile.

## Design

Add one narrow Tauri command that resolves a safe start directory before the dialog
opens. It evaluates candidates in this order:

1. the requested current workspace, if it exists and is a directory;
2. Tauri's resolved home directory, if it exists and is a directory;
3. the process current directory, if it exists and is a directory.

The command returns `null` only when none is usable. The frontend dialog boundary
invokes it in Tauri mode and passes only the returned existing directory as
`defaultPath`. Browser mode remains unchanged and does not invoke native APIs.

This approach adds no filesystem plugin and no broad filesystem capability. The
validation remains inside trusted Rust code, while the existing dialog plugin retains
responsibility for selection and cancellation.

## Error handling

- A deleted or non-directory workspace is ignored and replaced by a safe fallback.
- A missing smoke-profile Desktop is never used implicitly when home or current
  directory is available.
- Resolver or dialog invocation errors continue through the existing workspace-picker
  error state.
- Cancellation remains a non-error and does not change the current workspace.

## Verification

- Rust unit tests cover a valid requested directory, deleted/non-directory candidates,
  and fallback ordering.
- Frontend tests prove that the resolved safe path—not the stale requested path—is
  supplied to the dialog, and that browser mode invokes neither resolver nor dialog.
- Run the focused Vitest file, the complete GUI Vitest suite, TypeScript/Vite build,
  `cargo test`, `cargo check`, and task-owned `git diff --check`.

## Out of scope

- Changing Windows known-folder registry entries.
- Retaining smoke profiles after the application exits.
- Granting arbitrary frontend filesystem-read permissions.
