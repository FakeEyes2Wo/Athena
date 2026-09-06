"""Package a committed source tree plus an explicit, verified TESS model bundle.

No dataset, raw session logs, .env or unrelated untracked files are copied.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reproduction.tess.bundle import load_verified_metadata, sha256_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.exists() or output.with_suffix(output.suffix + ".sha256").exists():
        raise FileExistsError(f"release output already exists: {output}")
    source_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    # Ship the committed files, not an accidental mixture with pending edits.
    subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            "HEAD",
            "--",
            "reproduction/tess",
            "scripts/package_tess_release.py",
        ],
        cwd=root,
        check=True,
    )
    model_dir = root / "reproduction/tess/weights"
    metadata = load_verified_metadata(model_dir)
    relative_assets = [
        f"reproduction/tess/weights/{metadata['model_file']}",
        "reproduction/tess/weights/metadata.json",
        "reproduction/tess/weights/checksums.txt",
        "reproduction/tess/predictions/predictions__TESS_flare_win120min.csv",
    ]
    manifest = {
        "source_commit": source_commit,
        "task_id": "TESS_flare_win120min",
        "assets_sha256": {name: sha256_file(root / name) for name in relative_assets},
        "data_included": False,
        "historical_final_reproduced": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="athena-tess-archive-") as temp_dir:
        archive_path = Path(temp_dir) / "source.zip"
        subprocess.run(
            [
                "git",
                "archive",
                "--format=zip",
                f"--output={archive_path}",
                source_commit,
            ],
            cwd=root,
            check=True,
        )
        with zipfile.ZipFile(
            archive_path, "a", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name in relative_assets:
                archive.write(root / name, name)
            archive.writestr(
                "RELEASE_SOURCE.json", json.dumps(manifest, indent=2) + "\n"
            )
        with archive_path.open("rb") as source, output.open("xb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
    digest = sha256_file(output)
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{digest}  {output.name}\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "source_commit": source_commit,
                "archive_sha256": digest,
                "bytes": output.stat().st_size,
            }
        )
    )


if __name__ == "__main__":
    main()
