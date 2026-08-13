"""Atomic JSON persistence helpers shared across durable state stores."""

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    """Atomically write JSON ``payload`` to ``path`` (tmp + fsync + replace)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target
