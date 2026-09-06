# Standalone TESS dependency notices

This entry point is covered by the repository-root MIT LICENSE. Third-party
packages retain their own licenses; do not relabel their code or bundled native
libraries as Athena's MIT code. The table lists declared upstream licenses;
retain the license files supplied in installed wheels when packaging binaries.

| Package | Declared license / notice |
| --- | --- |
| NumPy | BSD-3-Clause plus bundled component notices |
| pandas | BSD license |
| XGBoost | Apache-2.0; retain bundled native-library notices |
| scikit-learn | BSD-3-Clause |
| SciPy | BSD license plus bundled component notices |
| joblib | BSD-3-Clause |
| threadpoolctl | BSD license |
| PyYAML | MIT |
| python-dateutil | BSD / Apache-2.0 |
| six | MIT |
| tzdata | Apache-2.0 package metadata; preserve bundled timezone-data notices |

Exact tested versions are in requirements.txt. Generate an installed inventory
in the chosen release environment with
`python ../../scripts/release_dependency_inventory.py` from this directory.
Native CUDA/runtime redistribution is not covered by this table; for GPU mode
use an appropriately provisioned system and retain its upstream terms.

The standalone path does not require Athena's qoder-agent-sdk, PyMuPDF,
OpenAI SDK, GUI fonts, or online research providers. See the repository-root
third_party_licenses.md for the broader developer-environment inventory and
remaining redistribution review. TESS data rights must be established separately.
