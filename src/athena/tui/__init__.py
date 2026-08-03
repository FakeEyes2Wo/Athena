"""Athena TUI — app_server 协议之上的终端界面。

对外只暴露装配所需的少数几个名字；渲染细节留在包内。
"""

from athena.tui.app import AthenaTUI
from athena.tui.approvals import ApprovalCoordinator, build_gate
from athena.tui.runner import AgentRuntime, MockRunner
from athena.tui.session import TuiSession
from athena.tui.state import AppState, Block, reduce_event
from athena.tui.theme import Theme, load_theme
from athena.tui.transcript import Transcript

__all__ = [
    "AthenaTUI",
    "AgentRuntime",
    "ApprovalCoordinator",
    "AppState",
    "Block",
    "MockRunner",
    "Theme",
    "Transcript",
    "TuiSession",
    "build_gate",
    "load_theme",
    "reduce_event",
]
