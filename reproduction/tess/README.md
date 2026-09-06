# TESS offline reproduction handoff

This borrows engineering requirements from the Agent open-source checklist,
not JW-FD's task definition. Athena research remains in `src/athena`; this
directory is the separate, non-interactive inference path for its TESS result.

**Engineering verification complete; owner sign-off still required.** A new
train-only XGBoost bundle has been exported locally. CPU inference on all
169,725 SEARCH feature rows exactly matches the historical SOTA hard predictions
by ID (zero differences). No evaluator labels or FINAL evaluation were used.
GitHub is the primary delivery channel; weights/predictions are local generated
assets, not included in a clone. Export them using the command below when needed.
See RELEASE.md for source identity. Team/contact, dataset
licensing and authorized distribution still require owner confirmation.

## Environment and commands

From the repository root, create a separate Python 3.11.6 environment and install
the versions in `reproduction/tess/requirements.txt`. Those are the new entry
point's verification environment, not a claim about the historical server.
The measured environment is Windows x64; Linux/macOS commands below illustrate
path syntax only, and those platforms have not been acceptance-tested here.

```bash
python -m venv .venv-tess
# Linux/macOS: .venv-tess/bin/python; Windows: .venv-tess/Scripts/python.exe
.venv-tess/bin/python -m pip install -r reproduction/tess/requirements.txt
.venv-tess/bin/python reproduction/tess/infer_batch.py \
  --data_root /path/to/split \
  --config reproduction/tess/config.example.yaml \
  --out /path/to/out/predictions__TESS_flare_win120min.csv
```

`weights/` must first contain the real model export and its checksums. Inference
does not train, download weights, use an API or ask interactive questions.
Change `input_file` to `final_features.csv` only for an authorized final-input
run. Missing model files are an error, not a reason to fall back to retraining.

If the original fitted model cannot be recovered, an explicit **new fit** can
be prepared using the train-only export command documented below. Its scores
must be measured separately; historical 0.9034/0.9000 must not be attributed to it.

### Explicit preparation of a new model

```bash
.venv-tess/bin/python reproduction/tess/export_model.py \
  --train_csv /path/to/split/train.csv \
  --model_dir reproduction/tess/weights --device cpu
```

This is training, not inference, and must be run separately before distribution.
It uses the supplied train labels only, original model settings and inherited
threshold 0.41; it does not access SEARCH/FINAL labels or reselect the threshold.
The resulting model is a new fit, not the missing historical fitted object.
This closeout performed one such new fit, not a new OOF/threshold search.

The bundle contains `model.json`, `metadata.json`, and `checksums.txt`.
Metadata freezes feature order, median-imputation state and threshold. Checksums
detect accidental changes; publish their trusted digest with the chosen release
to establish artifact identity. A mutable checksum next to a model is not a
cryptographic signature or a substitute for source/provenance review.

## Data and output contract

Input CSV columns:

```text
__athena_row_id,TIC,sector,w_index,
scatter_ppm,max_snr,n_above_1s,n_above_3s,max_run_above_1s,
sum_pos_resid,resid_skew,resid_kurt,slope_after_peak
```

Input is an entire feature partition. No `label` column or `*_labels.csv` is
required. Stable row IDs must be unique and non-null. Output contains each
input ID once with a binary `label`, preserving TESS's original CSV contract.
Scores are computed separately by an authorized evaluator, not the inference
entry point. The existing standalone CSV has hard labels, not recoverable
probabilities; never invent probability values for that historical file.

Feature derivation groups by TIC and sector, orders by w_index, adds lag/lead
features, and scales amplitude features by each TIC's median/IQR. Consequently
this is full-partition offline inference, not chunk-independent or causal
streaming inference. Full feature-table memory is required.

## Hardware, time and budget

- CPU mode is the portable default; CUDA mode requires working local XGBoost
  GPU support. No inference-time installation/download is performed.
- Historical task instructions mention a 32 GB NVIDIA vGPU. This is not a
  measured hardware requirement for the new entry point.
- Measured locally: RTX 4070 Laptop GPU (8,188 MiB), train-only export 8.72 s;
  CPU SEARCH inference 2.39 s including process startup, 169,725 rows.
  Exported model JSON is 4,053,886 bytes. Timings are single warm-machine runs,
  not an SLA. Peak RAM/VRAM and clean-install time were not measured.
- Offline inference LLM/API calls and tokens: **0**. API retry/rate limiting
  is not applicable to inference; local failures exit with an error.
- Athena research is a different workflow using `config.example.toml` and
  environment-only API keys. Its prompts are versioned under
  `src/athena/agents/prompts`; orchestration/tools under `src/athena`.
  It is not rerun by this inference command.
- Research example sampling settings: `LLM_TEMPERATURE=0.1`, optional
  `LLM_SEED=62` only for supporting APIs; these are not asserted to be the
  historical run's settings. SDK retry/timeout controls are in the existing
  TOML template. Provider quotas and total research token/call budget need
  separate measurement; a SEARCH count is not an API-call budget.
- No external vector database/cache is used by offline inference. Replaying
  an online literature search is not guaranteed to return a frozen corpus.

## Release checklist

- [x] Existing repository root MIT LICENSE and versioned Athena source/prompts.
- [x] TESS-specific task declaration and separate inference configuration.
- [x] New fitted model, imputer, checksums and train-hash/version provenance.
- [x] Real SEARCH-input run; complete ID coverage and zero differences against
  historical SOTA predictions. No fresh label-based metric recomputation.
- [ ] Dataset acquisition/version/license and permitted redistribution confirmed.
- [x] Full-data hardware and wall-clock measurement; peak memory not profiled.
- [ ] Team/contact and response window supplied by owner.
- [x] Use the repository URL and `git rev-parse HEAD` to identify the submitted
  source version; see RELEASE.md. A source ZIP is not required.

Historical and fresh-run evidence are separated in `logs/public_run_summary.md`.
Root `uv.lock` fixes the
Athena environment; the standalone environment is separately pinned.
See `third_party_licenses.md` for dependency inventory scope and limitations.

## Distribution safety

Do not zip the entire developer workspace or `.athena` logs blindly. Exclude
credentials, `.env`, caches, local agent sessions and nested `.git` directories.
Distribute a clean source checkout at the recorded commit plus an explicitly
audited model bundle and authorized data/predictions. Repository MIT terms do
not establish redistribution rights for TESS source data or third-party models.
