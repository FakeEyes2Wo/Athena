"""Inventory installed distribution versions/license metadata, without imports.

Run in the environment being distributed. Output is an inventory, not a legal
clearance or a guarantee that every wheel's bundled native library is listed.
"""

import importlib.metadata


def main() -> None:
    print("| Distribution | Installed version | Declared license metadata |")
    print("| --- | --- | --- |")
    for dist in sorted(
        importlib.metadata.distributions(),
        key=lambda d: d.metadata.get("Name", "").lower(),
    ):
        license_text = dist.metadata.get("License-Expression")
        if not license_text:
            classifiers = dist.metadata.get_all("Classifier", [])
            license_text = "; ".join(
                c.removeprefix("License :: ")
                for c in classifiers
                if c.startswith("License :: ")
            )
        if not license_text:
            license_text = dist.metadata.get("License", "UNKNOWN; review upstream")
        first_line = (
            license_text.strip().splitlines()[0] if license_text.strip() else "UNKNOWN"
        )
        fields = (dist.metadata["Name"], dist.version, first_line[:240])
        print("| " + " | ".join(f.replace("|", "/") for f in fields) + " |")


if __name__ == "__main__":
    main()
