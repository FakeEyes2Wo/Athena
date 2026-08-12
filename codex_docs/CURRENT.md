# Current Athena Work

Active implementation plan:
- `codex_docs/2026-08-12-supervisor-autonomous-search-implementation-plan.md`

Supporting technical design:
- `codex_docs/2026-08-12-supervisor-autonomous-search-technical-design.md`
- `codex_docs/优化设计.md`

Executable TDD supplement:
- `codex_docs/2026-08-12-supervisor-autonomous-search-execution-tdd.md`
- `codex_docs/2026-08-12-supervisor-search-runtime-tdd.md`
- `codex_docs/2026-08-12-supervisor-validation-output-restoration-tdd.md`
- `codex_docs/2026-08-12-supervisor-validate-tdd.md`
- `codex_docs/2026-08-12-supervisor-cleanup-acceptance-tdd.md`

Completion ledger:
- `codex_docs/COMPLETED.md`

Execution rules:
- Follow the active plan task by task and update its checkboxes as evidence is produced.
- A task Agent reads only its claim row, the execution index's global contract,
  its assigned TDD packet, and the exact technical-design sections named by
  that packet. Do not read another task's packet for context.
- The active plan is currently owned by Codex agent `/root`; do not claim or implement it concurrently.
- Every implementation Agent must work directly in the shared `main` checkout. Do not create, switch to, or modify another branch or worktree for this plan.
- An Agent may claim exactly one task in this plan. A task may be claimed and modified by exactly one Agent.
- `/root` is the only coordinator allowed to fill, transfer, or clear Agent claims in the active plan. An empty claim means no Agent may edit that task's files.
- Tasks in the same documented set may run concurrently only after their individual claims are filled and their file ownership does not overlap.
- Task Agents edit only their claimed product and test files. `/root` serializes plan updates, staging, and commits so the shared Git index cannot mix tasks.
- `/root_2_u` is the sole completion-maintenance Agent. After independently verifying a task is complete, it records the task in `codex_docs/COMPLETED.md`, removes that completed task from the active plan's claim/task lists, and may delete temporary `finished_*.md` files whose evidence has been preserved. It must not modify product code or remove unfinished tasks.
- Every task must complete its infrastructure-first audit before adding or replacing an interface.
- Fresh red-green-regression evidence completes a task; no separate implementation review is required.
- After all acceptance criteria have fresh evidence, write the final report, delete the implementation plan, and update this file so it no longer points to the completed plan.
