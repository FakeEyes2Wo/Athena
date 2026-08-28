"""Backward-compatible PlanAgent registration import.

The implementation now lives with the other thin prompt-driven task agents in
``task_agents``.  Keep this module as an import shim for existing integrations.
"""

from athena.agents.task_agents import PLAN_AGENT_TYPE, register_plan_agent

__all__ = ["PLAN_AGENT_TYPE", "register_plan_agent"]
