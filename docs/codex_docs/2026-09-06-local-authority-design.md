# Local baseline authority mode

## Decision

Provide an explicit local authority mode for single-machine GUI use. It is the
default only when no host-owned controller factory is configured. The existing
external `BaselineAuthorityStore` protocol stays unchanged so remote authority
can be added later without changing PREPARE or research artifacts.

This is an intentional security downgrade: local mode provides durable recovery,
atomic compare-and-exchange and accidental-corruption detection, but it does
not protect against a process with the same OS account and file permissions.
The GUI and events must state that mode; it must never be called remote or
tamper-resistant authority.

## Local implementation

- Add `LocalBaselineAuthorityStore` under `research/prepare/authority_local.py`.
  It stores exactly one `SealedBaseline` per project/session under the
  controller state root, not the agent workspace or `.athena` artifacts.
- Store raw bundle bytes as Base64 plus parsed verification and attestation in
  one JSON record. Validate all decoded objects through existing immutable
  authority dataclasses before returning them.
- Use an OS file lock and write a temporary sibling file followed by replace.
  `seal` and `attest_prepare` re-read beneath the lock and enforce exact
  generation compare-and-exchange. Corrupt/malformed records raise
  `BaselineAuthorityError`; they do not silently reset.
- Restrict filenames to a SHA-256 namespace derived from resolved project path
  plus session ID. No task text, user-provided filename or agent path selects
  authority storage.

## GUI configuration

`ATHENA_AUTHORITY_MODE` accepts only `local` or `ssh`.

- Unset or `local`: construct the local store beneath a controller-private
  root. `ATHENA_AUTHORITY_LOCAL_ROOT` can override that root for deployment;
  default is a user-local Athena controller directory, never a project path.
- `ssh`: deliberately raises an actionable “not implemented” preflight error.
  It must not silently fall back to local. The documented future SSH protocol
  remains `load`, `seal` and `attest_prepare` over a configured `~/.ssh/config`
  host alias, with remote credentials and storage outside Agent reach.
- A host `ATHENA_CONTROLLER_FACTORY` still has priority over local mode. It may
  supply a remote/custom authority and bounded repair callbacks.

The GUI settings snapshot exposes only `authority_mode` and a plain-language
security level; no authority root, SSH host, credentials or callbacks are sent
to the browser.

## General repair policy

The existing bounded repair role remains for controller-factory deployments
whose authority has temporary network failure. Local mode never dispatches it:
local I/O, corruption and lock failures are reported as nonrepairable setup
failures. This prevents an Agent from “repairing” local authority records.

## Acceptance and tests

- Fresh local project/session can seal, reload and attest exact bytes across a
  new store instance.
- Stale generation, malformed JSON and corrupt Base64 fail closed without
  erasing the record.
- Two project/session identities do not share a file.
- GUI defaults to local, reports its security level, honors host factory
  priority, and rejects `ssh` instead of falling back.
- Existing external-factory and bounded-repair tests remain green.

No real SSH command, key, host probe, remote server, dataset or FINAL read is
in scope. Migration is additive: existing external deployments retain their
factory; a project without an external record starts a new local generation.
