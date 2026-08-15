"""Idea Generation light pipeline: pre_gate (structural + falsifiability) ->
methodology/statistics review -> light_hard_gate, then survivors are returned in
submission order to the shared ResearchTree/Supervisor hypothesis pool (no local ranking).

Ported from feature/idea-generation-pre-gate onto this branch's Supervisor/ResearchRuntime
architecture. Orchestration is plain asyncio (gather/semaphores); no langgraph dependency.

The production entry point is ``gate.run_light_pipeline``, called by
``research/agent_turn_runner._finish_ideator_batch`` when ``--ideation ideageneration``
(the default). ``--ideation debate`` routes to the debate-based Ideator instead.
"""
