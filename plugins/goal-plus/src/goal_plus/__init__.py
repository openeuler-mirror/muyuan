"""Goal Plus runtime with optional verifier-guided Search Mode."""

from goal_plus._version import __version__
from goal_plus.domain.evidence import (
    IterationRecord,
    ScoreReport,
    VerifierResult,
)
from goal_plus.domain.promotion import (
    MainAgentNotification,
    PendingIntegrationRecord,
    PublicationOutcome,
    PublicationRecoveryRecord,
)
from goal_plus.domain.search import (
    AgentSessionRecord,
    Budget,
    CandidateProposal,
    CandidateTask,
    CandidateWorkOrder,
    EditSurface,
    FeedbackPolicy,
    FrozenSpec,
    RunState,
    RunSummary,
    SearchPlan,
    SearchSpec,
    StrategySpec,
    VerifierCommand,
    VerifierRole,
)
from goal_plus.ports.execution import WorkerExecutionResult
from goal_plus.ports.state import (
    CandidateRecord,
    RunRecord,
)


def __getattr__(name: str):
    # Keep the public runtime import without loading execution for schema users.
    if name == "FileSearchRuntime":
        from goal_plus.runtime import FileSearchRuntime

        return FileSearchRuntime
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

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
    "MainAgentNotification",
    "PendingIntegrationRecord",
    "PublicationOutcome",
    "PublicationRecoveryRecord",
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
    "WorkerExecutionResult",
]
