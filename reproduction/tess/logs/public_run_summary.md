# Sanitized TESS run evidence — 2026-09-06

## Fresh engineering run (not the historical fitted object)

- Interpreter: Python 3.11.6, Windows; exact packages in requirements.txt.
- GPU detected locally: NVIDIA GeForce RTX 4070 Laptop GPU, 8,188 MiB.
- Train-only export: 507,789 rows, original fixed 800-tree XGBoost settings,
  seed 0, frozen threshold 0.41; wall-clock 8.7156375 s, exit 0.
- New model JSON: 4,053,886 bytes; SHA256
  `1c821bb24e86fcdb9b28ed311ee54470ba585ccebd87b11fcfa6b90c5b9d8b5b`.
- metadata.json SHA256:
  `17c2b2a2ed04be6a5521ef6977228dfe8e660efcd68b0977b2063517958b67db`.
- Model metadata records train-file SHA256 and actual package versions.
- CPU offline SEARCH inference: 169,725 rows, wall-clock 2.3889667 s, exit 0.
- Input `search_features.csv` SHA256:
  `65d0fd9e9647ca1f381c7c29af91a024c86dad2281948798c2b69c7f9114599`.
- New prediction CSV SHA256:
  `fc2a03966cee7174e34a72874168b5ff044395f8a6a4b6328d27d37c552cc5db`.
- Output columns `__athena_row_id,label`; 169,725 unique IDs; all input IDs
  covered exactly once; zero differences from the historical SOTA label vector
  after a one-to-one ID join. Byte hashes differ because output ordering differs.
- No SEARCH evaluator labels, FINAL inputs/labels, online API, LLM, or model
  downloads were used by inference. Macro F1 was not freshly recomputed from
  labels; prediction equivalence is the evidence linking this output to the
  historical reference. No fresh FINAL score is claimed.
- Timings include process startup on a warm machine, exclude environment
  installation and do not measure peak memory or establish resource guarantees.

## Historical reference, kept separate

Sources: supplied comparison package SOTA xgb_results.json, prediction CSV,
Athena state.json and clarification.json. No raw session logs are republished.

| Quantity | Recorded value | Meaning |
| --- | ---: | --- |
| Standalone train OOF macro F1 | 0.901048311120028 | Train-only threshold selection |
| Athena SEARCH macro F1 | 0.9034041233494992 | Historical native evaluation |
| Athena FINAL macro F1 | 0.9000079841996891 | Historical native evaluation only |
| Generalization gap | 0.0033961391498100735 | Historical warning retained |

Reference SOTA CSV SHA256:
`a66f451ddf2ad1e887d0b61424fbbf04d5ea03c417d57a0220ad1cb010134ee4`.
It has 169,725 unique IDs and 100% coverage of the available SEARCH features.
Artifacts whose content is a reference manifest have different hashes from
this CSV; do not confuse an artifact reference hash with a prediction-file hash.

The original task explicitly acknowledges historical FINAL exposure. This is
not a pristine blind-test claim. Historical task text mentions CUDA, XGBoost
3.2.0 and a 32 GB NVIDIA vGPU, but these were not independently corroborated as
the exact historical installed environment. Fresh exported metadata is separate.

## Commands (portable path placeholders)

```bash
python reproduction/tess/export_model.py --train_csv /data/train.csv --model_dir reproduction/tess/weights --device cuda
python reproduction/tess/infer_batch.py --data_root /data --config reproduction/tess/config.example.yaml --out reproduction/tess/predictions/predictions__TESS_flare_win120min.csv
python -m pytest reproduction/tess/tests -q -p no:cacheprovider
```

The focused synthetic tests are not counted as TESS metric runs. They check
label-free inference, ordering/ID preservation and checksum rejection. An
additional direct comparison against historical source functions confirmed the
25-feature order and derived matrix on a shuffled synthetic partition.
