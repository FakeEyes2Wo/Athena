"""Backward-compatible GeneralAgent registration import.

The implementation is consolidated in ``task_agents``; this module preserves
the public import path used by existing integrations and contract tests.
"""

from athena.agents.task_agents import (
    GENERAL_AGENT_TYPE,
    GeneralResult,
    register_general_agent,
)

__all__ = ["GENERAL_AGENT_TYPE", "GeneralResult", "register_general_agent"]
