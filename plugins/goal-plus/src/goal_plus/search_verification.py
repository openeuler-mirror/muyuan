"""Host-driven verifier and Scheduler barriers for Search state workflows."""

from collections.abc import Callable, Generator
from typing import TypeVar

from goal_plus.domain.evidence import ScoreReport
from goal_plus.ports.candidate_verification import CandidateVerificationRequest

Result = TypeVar("Result")


def resolve_verification_requests(
    workflow: Generator[CandidateVerificationRequest, ScoreReport, Result],
    verify: Callable[..., ScoreReport],
) -> Result:
    try:
        request = next(workflow)
        while True:
            try:
                report = verify(
                    request.run_id, request.candidate_id, scope=request.scope
                )
            except BaseException as exc:
                request = workflow.throw(exc)
            else:
                request = workflow.send(report)
    except StopIteration as completed:
        return completed.value
    finally:
        workflow.close()
