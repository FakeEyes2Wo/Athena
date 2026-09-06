"""Controller authority selection; never populated by GUI requests or agents."""

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from athena.research.prepare.authority import BaselineAuthorityStore
from athena.research.prepare.authority_local import LocalBaselineAuthorityStore

_AUTHORITY_MODE_ENV = "ATHENA_AUTHORITY_MODE"
_AUTHORITY_ROOT_ENV = "ATHENA_AUTHORITY_LOCAL_ROOT"


def _default_local_root() -> Path:
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            / "Athena"
            / "controller-authority"
        )
    return Path.home() / ".local" / "share" / "athena" / "controller-authority"


@dataclass(frozen=True)
class ControllerCapabilities:
    """One session's external authority and pre-approved environment operations.

    The host owns service credentials and operation implementations. Callbacks
    must not expose arbitrary commands, paths, code editing, or secret output.
    """

    baseline_authority: BaselineAuthorityStore
    repair_actions: Mapping[str, Callable[[], Awaitable[None]]] = field(
        default_factory=dict
    )


ControllerFactory = Callable[[Path, str], ControllerCapabilities]


def local_controller_capabilities(
    project_root: Path, session_id: str
) -> ControllerCapabilities:
    """Build the explicit same-user fallback used only without a host factory."""
    mode = os.environ.get(_AUTHORITY_MODE_ENV, "local").strip().lower() or "local"
    if mode == "ssh":
        raise RuntimeError(
            "SSH baseline authority is not implemented; set ATHENA_AUTHORITY_MODE=local "
            "or configure ATHENA_CONTROLLER_FACTORY"
        )
    if mode != "local":
        raise RuntimeError("ATHENA_AUTHORITY_MODE must be 'local' or 'ssh'")
    configured_root = os.environ.get(_AUTHORITY_ROOT_ENV, "").strip()
    root = (
        Path(configured_root).expanduser() if configured_root else _default_local_root()
    )
    return ControllerCapabilities(
        baseline_authority=LocalBaselineAuthorityStore(root, project_root, session_id),
    )
