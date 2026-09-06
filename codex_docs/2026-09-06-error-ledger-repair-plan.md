# Error-ledger repairs

Scope: user-approved repairs grounded in ATHENA_ERRORS.md and current Python implementation. Preserve unrelated edits; exclude athena_ts. Reuse existing tests and add only failure-chain coverage. Do not use superpowers skills.

- [x] Bound process cancellation and clean up process groups/readers.
- [x] Resolve prediction features from the frozen contract and report missing required inputs.
- [x] Surface bounded baseline diagnostics and align schema instructions.
- [x] Add durable per-Plan cancellation through existing Supervisor ownership and command routing.
- [x] Correct agent-commit change detection without breaking reviewed Git commits.
- [ ] Verify guidance recovery, deployment isolation constraints, and remaining ledger findings; implement supported fixes and explicitly report evidence gaps.
- [ ] Run focused checks, record completion evidence, commit and push completed changes to main; remove only task-created temporary branches after integration.

Existing transactional-backend and simplification plans remain separate unfinished work. Do not mark their future architecture as already implemented. Remove this plan and update CURRENT only when every item above is verified or explicitly resolved with the user.

Evidence and remaining limitations: `codex_docs/2026-09-06-error-ledger-repair-progress.md`.
