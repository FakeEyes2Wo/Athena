"""Explicit host-owned capabilities; never populated by GUI requests or agents."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from athena.research.prepare.authority import BaselineAuthorityStore


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
