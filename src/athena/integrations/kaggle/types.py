"""Kaggle integration types."""
from dataclasses import dataclass


@dataclass
class CompetitionInfo:
    name: str = ""
    type: str = ""
    metric: str = ""
    deadline: str = ""
    prize: str = ""
    description: str = ""
    category: str = ""


@dataclass
class CompetitionSummary:
    ref: str = ""
    title: str = ""
    category: str = ""
    deadline: str = ""


@dataclass
class SubmissionResult:
    submission_id: str = ""
    status: str = ""
    public_score: float | None = None


@dataclass
class SubmissionStatus:
    submission_id: str = ""
    status: str = ""
    message: str = ""


@dataclass
class LeaderboardEntry:
    rank: int = 0
    team: str = ""
    score: float = 0.0
