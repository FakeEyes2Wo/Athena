# TESS task declaration

Status: engineering handoff with real SEARCH prediction parity verified;
owner distribution/sign-off requirements remain open.

- Project: Athena; task ID: `TESS_flare_win120min`.
- Team name, contact/email, response window: owner must supply before submission.
- Task: binary stellar-flare classification of 120-minute TESS windows;
  this is not JW-FD solar-flare forecasting and does not claim a 24-hour horizon.
- Input: an entire feature partition with `__athena_row_id`, `TIC`, `sector`,
  `w_index` and the nine morphology columns listed in README.
- Target: existing dataset `label` (0/1). Positive class means the dataset's
  flare-window label. Event-to-window labeling specification and dataset
  redistribution permission must accompany a real release; they are not
  reconstructed or asserted from model code.
- Output: `__athena_row_id,label`, preserving the original TESS prediction
  contract; no invented `image_filename` mapping.
- Primary metric: sklearn `f1_score(average="macro", labels=[0,1])`.
- Threshold: `0.41`, inherited from the recorded standalone train-only grouped
  OOF selection. The export utility freezes this value and does not tune it.
- Original threshold procedure: five TIC-group-disjoint folds, seed 0,
  inclusive 0.40–0.60 step 0.01, maximize mean fold macro F1 minus 0.25 times
  population standard deviation; ties retain the lowest threshold.
- Model: XGBoost, hist, seed 0, 800 trees, learning rate 0.02, depth 6,
  subsample/colsample 0.9, lambda 1, alpha 0.1, scale_pos_weight 1.
- Preprocessing: sequence lag/lead morphology, within-TIC median/IQR scaling,
  train-fitted median imputation, fixed 25-column feature order.
- Preprocessing uses the entire requested feature partition, including future
  neighboring windows. This is offline window classification, not causal
  streaming forecasting. Do not split arbitrary chunks before deriving features.
- The new inference entry point needs neither labels nor training data.
  It loads only a frozen local model bundle; no network or agent calls.
- Historical research logs explicitly acknowledge prior FINAL exposure.
  No pristine blind-test or fresh independent generalization claim is made.
- Historical SEARCH/FINAL scores are provenance only. Newly fitted/exported
  models require their own prediction and metric verification.

See README for commands and release blockers. No signature or compliance
certification is supplied on behalf of the project owner.
