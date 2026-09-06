"""Check tracked/release text for credential patterns without echoing values.

Heuristic only: this does not audit Git history, archives, binary artifacts or
all possible credential formats. A match needs private review, not automatic
deletion. Pass explicit files to additionally check untracked release material.
"""

import argparse
from pathlib import Path
import re
import subprocess

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "provider-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    "github-token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"
    ),
    "cloud-access-id": re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()
    paths = args.files or subprocess.check_output(
        ["git", "ls-files", "-z"], text=True, encoding="utf-8"
    ).split("\0")
    checked = skipped = matches = 0
    for name in paths:
        path = Path(name)
        if not name or not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            skipped += 1
            continue
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            skipped += 1
            continue
        checked += 1
        for lineno, line in enumerate(content.splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    print(f"REVIEW {path}:{lineno} ({kind}; value redacted)")
                    matches += 1
    print(
        f"Checked {checked} text files; skipped {skipped} binaries/non-UTF8; matches {matches}."
    )
    return 1 if matches else 0


if __name__ == "__main__":
    raise SystemExit(main())
