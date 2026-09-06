# TESS release identity and remaining sign-off

The local source-and-model archive is produced with:

```bash
python scripts/package_tess_release.py --output /path/to/tess_reproduction_bundle.zip
```

It contains the exact committed Git source tree, the new fitted model and
metadata/checksums, the real SEARCH prediction CSV, and `RELEASE_SOURCE.json`.
That JSON records the immutable **source commit** and asset SHA256 values;
the archive's sibling `.sha256` file identifies the complete distribution.
Do not use a moving `main` branch name as the submission's source identity.

The handoff for this run is written to the sibling `submit_material` directory
as `tess_reproduction_bundle.zip`. Git contains source and documentation only;
model/predictions are deliberately local bundle assets, not silently omitted.
The archive does not add untracked data, raw logs, nested repositories or `.env`.
The credential scan covers known patterns in text, not a complete security audit.
The packaging tool includes only four explicitly selected runtime assets.

Owner sign-off still needed:

- Team name, contact/email and response window.
- Dataset version/acquisition instructions and redistribution authorization.
- Review third-party notices for any separately bundled Athena runtime or GUI.
- Fresh authorized metric recomputation if required by the recipient; current
  evidence is full ID/label parity with the historical SEARCH prediction vector.
- Choose the final archive/source identity when submitting; do not claim a
  fresh FINAL evaluation or pristine blind-test history.

Source integration policy: commit scoped changes on main, push, and remove
only task-created temporary branches. This task creates no temporary branch.
