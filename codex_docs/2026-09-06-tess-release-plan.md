# TESS engineering release closeout

## Scope and decision

User confirms TESS only; borrow engineering requirements from
`../submit_material/05_智能体开源与复现规范.md`, not JW-FD task IDs or metrics.
Keep `TESS_flare_win120min`, `__athena_row_id,label`, macro F1, and historical
scores. Build a standalone `reproduction/tess/` delivery surface, without
changing Athena research scheduling or raw comparison snapshots.

## Tasks and acceptance

- [x] Inspect available fitted models, inputs, predictions and provenance.
- [x] Implement offline `infer_batch.py --data_root --config --out`, reusable
  feature construction, and explicit train-only model export. Inference must
  never train, call an API, download a model, or require labels. Model includes
  median-imputation state, feature order, frozen threshold and checksums.
- [x] Add pinned environment, config example, task declaration, dependency
  notices, sanitized historical run summary and precise launch instructions.
  Do not invent contact details, licenses, hardware, timing or measured scores.
- [x] Verify with focused synthetic label-free tests and comparison to source
  feature functions; inspect release material for credentials without printing
  secret values. Distinguish smoke tests from full TESS replication.
- [ ] Record immutable source commit and integrate scoped changes on main;
  preserve unrelated worktree content and do not create unnecessary branches.
- [x] Full-data acceptance: real frozen model supplied/exported, checksums and
  pinned runtime established, successful public-input prediction with coverage
  and metrics reconciled to its own predictions. New fit/export 8.72 s; real
  SEARCH inference 2.39 s, all 169,725 IDs covered and zero label-vector
  differences from reference. Historical metric retained as historical;
  no fresh evaluator-label access. Owner contact/data rights remain sign-off items.

## Ownership and boundaries

Implementation worker: reproduction/tess Python modules and focused tests.
Evidence worker: read-only asset/provenance inspection, no label reads.
Parent: plan, README/config/dependency documentation and delivery integration.
No blanket repository cleanup, hidden-label reads, or claimed reproduction of
historical FINAL scores. No remote publishing of datasets.
Rollback is removal/revert of task-owned files; original demo source untouched.
Only delete this plan once all acceptance criteria have fresh evidence.

## Asset discovery update

Read-only inspection found actual train/search feature inputs under
`../test/athena-tess-strict-macro-f1-v2-20260905/workspaces/data_split`.
For the requested TESS closeout, prepare one new fixed-parameter train-only
model after focused tests, then run the real unlabeled SEARCH features. This
supersedes the initial no-full-training assumption. Preserve historical model
claims and do not access FINAL or SEARCH evaluator labels directly. Record
new artifact identity, timing and input coverage separately from old scores.
