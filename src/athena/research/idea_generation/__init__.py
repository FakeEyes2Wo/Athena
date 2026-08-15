"""Idea Generation pipeline: hypothesis generation, structural/falsifiability gating,
novelty audit, multi-perspective review, and the REVISE debate loop.

Ported from feature/idea-generation-pre-gate onto this branch's Supervisor/ResearchRuntime
architecture.

Orchestration note: the old branch's langgraph StateGraph orchestration is NOT ported —
this branch has no langgraph dependency and adding one is out of scope under time pressure.
``workflow.run_full_pipeline`` uses plain asyncio (gather/semaphores), matching the old
branch's own pre-langgraph implementation.
"""
