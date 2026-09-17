"""Steering policy component and one-shot Executor API; disabled unless configured."""

import logging

from goal_plus.application.steering_apply import SteeringExecutor
from goal_plus.application.steering import SteeringDiagnostics
from goal_plus.domain.adaptive.models import SteeringDecision, SteeringMessage
from goal_plus.domain.adaptive.policy import SteeringPolicySpec
from goal_plus.domain.verifier import VerifierRecord
from goal_plus.ports.steering import SteeringHost, SteeringResult
from goal_plus.projections.steering import DIAGNOSTIC_RENDERER_VERSION, render_diagnostic_message


class Steering:
    def __init__(self, *, executor: SteeringExecutor, diagnostics: SteeringDiagnostics) -> None:
        self._executor, self._diagnostics = executor, diagnostics

    def accept(self, record: VerifierRecord, *, host: SteeringHost,
               policy: SteeringPolicySpec | None = None) -> SteeringResult:
        try:
            if record.outcome != "completed":
                return SteeringResult("skipped", "failed_record")
            if policy is None:
                return SteeringResult("skipped", "disabled")
            return self._diagnose(record, host=host, policy=policy)
        except Exception as exc:
            result = SteeringResult("error", "policy_or_execution_error", error_type=type(exc).__name__)
        logging.getLogger(__name__).info("Steering record=%s result=%s error_type=%s",
                                        record.record_id, result.reason_code, result.error_type)
        return result

    def _diagnose(self, record, *, host, policy):
        input, evaluation = self._diagnostics.evaluate(record, policy=policy,
                                                       renderer_version=DIAGNOSTIC_RENDERER_VERSION)
        if evaluation.action is None:
            result = SteeringResult("skipped", evaluation.reason_code)
        else:
            decision = SteeringDecision(source_record_id=record.record_id,
                subject_hash=input.subject_hash, candidate_id=input.candidate_id,
                message=SteeringMessage(text=render_diagnostic_message(input, evaluation)),
                reason_code=evaluation.reason_code, issue_key=input.issue_key,
                evidence_revision=input.evidence_revision, input_hash=input.input_hash)
            result = self._executor.steer(record, decision, policy=policy.identity,
                delivery=policy.delivery, renderer_version=DIAGNOSTIC_RENDERER_VERSION,
                host=host, limits=policy.parameters)
        try:
            self._diagnostics.observe(record, policy=policy,
                renderer_version=DIAGNOSTIC_RENDERER_VERSION, result=result)
        except Exception as exc:
            # Audit failure cannot reclassify a completed send or backpressure Verifier delivery.
            logging.getLogger(__name__).warning("Steering observation unavailable record=%s error_type=%s",
                                                record.record_id, type(exc).__name__)
        logging.getLogger(__name__).info("Steering record=%s action=%s mode=%s result=%s",
            record.record_id, evaluation.reason_code, policy.delivery.mode, result.reason_code)
        return result


__all__ = ["Steering", "SteeringExecutor"]
