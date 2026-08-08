"""兼容层：``athena.ideator.types`` → ``athena.agents.ideator.types``。"""

from athena.agents.ideator.types import (  # noqa: F401
    DebateResult,
    _Candidate,
    _Critique,
    _Draft,
    _JudgeDecision,
    _JudgeOutput,
    _JudgedDraft,
    _ProposalBatch,
    _ReviewBatch,
    _RevisionBatch,
    _TurnRequest,
)
