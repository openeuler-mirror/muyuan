"""Goal Plus runtime with optional verifier-guided Search Mode."""

from goal_plus._version import __version__
from goal_plus.models import (
    AgentSessionRecord,
    Budget,
    CandidateProposal,
    CandidateRecord,
    CandidateTask,
    CandidateWorkOrder,
    EditSurface,
    FeedbackPolicy,
    FrozenSpec,
    IterationRecord,
    RunRecord,
    RunState,
    RunSummary,
    ScoreReport,
    SearchPlan,
    SearchSpec,
    StrategySpec,
    VerifierCommand,
    VerifierResult,
    VerifierRole,
)
from goal_plus.adaptive_search.adaptive_search import (
    bind_search_scheduler_schemas as _bind_search_scheduler_schemas,
)

_bind_search_scheduler_schemas()
del _bind_search_scheduler_schemas

from goal_plus.runtime import FileSearchRuntime

__all__ = [
    "__version__",
    "AgentSessionRecord",
    "Budget",
    "CandidateProposal",
    "CandidateRecord",
    "CandidateTask",
    "CandidateWorkOrder",
    "EditSurface",
    "FeedbackPolicy",
    "FileSearchRuntime",
    "FrozenSpec",
    "IterationRecord",
    "RunRecord",
    "RunState",
    "RunSummary",
    "ScoreReport",
    "SearchPlan",
    "SearchSpec",
    "StrategySpec",
    "VerifierCommand",
    "VerifierResult",
    "VerifierRole",
]
