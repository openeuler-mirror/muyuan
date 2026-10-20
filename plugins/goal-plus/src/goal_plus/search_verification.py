"""Host-driven hard-verifier requests for Search state workflows."""

from collections.abc import Callable, Generator
from typing import TypeVar

from goal_plus.domain.evidence import ScoreReport
from goal_plus.ports.candidate_verification import CandidateVerificationRequest

Result = TypeVar("Result")


def resolve_verification_requests(
    workflow: Generator[CandidateVerificationRequest, ScoreReport, Result],
    verify: Callable[..., ScoreReport],
    *, recheck: Callable[[CandidateVerificationRequest], ScoreReport] | None = None,
) -> Result:
    try:
        request = next(workflow)
        while True:
            try:
                if request.frozen_spec_id is not None:
                    if recheck is None:
                        raise RuntimeError("selection recheck requires its trusted verifier")
                    report = recheck(request)
                else:
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
