"""Spend one sending opportunity, try immediately, and never recover or resend it."""

from goal_plus.domain.adaptive.models import (
    SteeringAttempt, SteeringDecision, SteeringDeliverySpec, SteeringPolicyIdentity,
    SteeringTarget, steering_attempt_key,
    SteeringLimits, steering_limit_reason,
)
from goal_plus.domain.serialization import sha256_text
from goal_plus.domain.time import timestamp_epoch
from goal_plus.domain.verifier import VerifierRecord, verifier_subject_identity
from goal_plus.ports.clock import Clock
from goal_plus.ports.steering import (
    SteeringAuthorization, SteeringHost, SteeringQueries, SteeringResult, SteeringState,
)


class _SourceRejected(ValueError):
    """A named refusal owned only by steering admission."""


class SteeringExecutor:
    def __init__(self, *, state: SteeringState, authorization: SteeringAuthorization,
                 queries: SteeringQueries, clock: Clock) -> None:
        self.state, self.authorization, self.queries, self.clock = state, authorization, queries, clock

    def _source_session(self, record: VerifierRecord, delivery: SteeringDeliverySpec):
        subject, receipt = record.subject, record.subject.settlement
        self.authorization.require_search_execution(subject.run_id)
        now = self.clock.now()
        created = timestamp_epoch(receipt.created_at)
        if created is None or not 0 <= now - created <= delivery.max_source_age_seconds:
            raise _SourceRejected("stale_source")
        if receipt.rollback is not None:
            raise _SourceRejected("rolled_back_source")
        run = self.state.read_run(subject.run_id)
        if run.frozen_spec_id != subject.frozen_spec_id:
            raise _SourceRejected("source_contract_mismatch")
        candidate = self.state.read_candidate(subject.run_id, subject.candidate_id)
        latest = max(candidate.iterations, key=lambda row: row.iteration, default=None)
        if (latest is None or latest.iteration != receipt.iteration or latest.settlement_receipt != receipt
                or not latest.agent_session_id
                or candidate.results_ledger_artifact_ref != receipt.ledger_artifact_ref):
            raise _SourceRejected("source_no_longer_current")
        session = self.queries.require_native_execution(latest.agent_session_id, now=now)
        if (session.run_id != subject.run_id or session.candidate_id != subject.candidate_id
                or session.execution_generation != candidate.execution_generation):
            raise _SourceRejected("source_session_mismatch")
        return session

    @staticmethod
    def _matches_session(target: SteeringTarget, session) -> bool:
        return (target.agent_session_id == session.agent_session_id
                and target.execution_generation == session.execution_generation
                and target.native_session_id == session.session_handle.external_id)

    def steer(self, record: VerifierRecord, decision: SteeringDecision, *,
              policy: SteeringPolicyIdentity, delivery: SteeringDeliverySpec,
              renderer_version: str, host: SteeringHost,
              limits: SteeringLimits | None = None) -> SteeringResult:
        if (record.outcome != "completed" or decision.source_record_id != record.record_id
                or decision.subject_hash != verifier_subject_identity(record.subject)
                or decision.candidate_id != record.subject.candidate_id):
            return SteeringResult("skipped", "source_mismatch")
        # The verifier -> allocation -> steering pipeline is single-dispatch.
        # Keep the lock in the state adapter so application code depends only
        # on the port and later attempts for one candidate remain valid.
        with self.state.lock_dispatch():
            return self._steer_once(record, decision, policy=policy, delivery=delivery,
                                    renderer_version=renderer_version, host=host, limits=limits)

    def _steer_once(self, record: VerifierRecord, decision: SteeringDecision, *,
              policy: SteeringPolicyIdentity, delivery: SteeringDeliverySpec,
              renderer_version: str, host: SteeringHost,
              limits: SteeringLimits | None = None) -> SteeringResult:
        key = None
        try:
            if (record.outcome != "completed" or decision.source_record_id != record.record_id
                    or decision.subject_hash != verifier_subject_identity(record.subject)
                    or decision.candidate_id != record.subject.candidate_id):
                return SteeringResult("skipped", "source_mismatch")
            key = steering_attempt_key(record.record_id, record.subject, policy, delivery, renderer_version)
            run_id = record.subject.run_id
            # No host lock while holding run.lock: host execution takes the opposite order.
            with self.state.lock_run(run_id):
                if self.state.read_attempt(run_id, key) is not None:
                    return SteeringResult("skipped", "already_consumed", key)
            session = self._source_session(record, delivery)
            target = host.active_call(session)
            if target is None:
                return SteeringResult("skipped", "no_active_call", key)
            with self.state.lock_run(run_id):
                session = self._source_session(record, delivery)
                if not self._matches_session(target, session):
                    return SteeringResult("skipped", "target_changed", key)
                if limits is not None:
                    if not decision.issue_key or not decision.evidence_revision or not decision.input_hash:
                        return SteeringResult("skipped", "missing_diagnostic_identity", key)
                    reason = steering_limit_reason(self.state.list_attempts(run_id, decision.candidate_id),
                        issue_key=decision.issue_key, evidence_revision=decision.evidence_revision,
                        now=self.clock.now(), parameters=limits)
                    if reason is not None:
                        return SteeringResult("skipped", reason, key)
                if delivery.mode == "shadow":
                    return SteeringResult("skipped", "shadow", key, target=target)
                attempt = SteeringAttempt(
                    attempt_key=key, source_record_id=record.record_id, source=record.subject,
                    policy=policy, delivery=delivery, target=target, message=decision.message,
                    message_hash=sha256_text(decision.message.text), renderer_version=renderer_version,
                    consumed_at=self.clock.timestamp(),
                    issue_key=decision.issue_key, evidence_revision=decision.evidence_revision,
                    input_hash=decision.input_hash, action=decision.reason_code,
                )
                if not self.state.consume_attempt(attempt):
                    return SteeringResult("skipped", "already_consumed", key)
        except _SourceRejected as exc:
            return SteeringResult("skipped", str(exc), key)
        except Exception as exc:
            # Including storage failures: a soft suggestion cannot backpressure delivery.
            return SteeringResult("error", "admission_failed", key, type(exc).__name__)

        rejected = None
        def before_send() -> None:
            nonlocal rejected
            try:
                with self.state.lock_run(run_id):
                    current = self._source_session(record, delivery)
                    if not self._matches_session(target, current):
                        raise _SourceRejected("target_changed")
            except Exception as exc:
                reason = str(exc) if isinstance(exc, _SourceRejected) else "pre_send_admission_failed"
                rejected = SteeringResult("skipped", reason, key, type(exc).__name__, target)
                raise

        observation = "returned"
        try:
            host.message(target, decision.message.text, timeout=delivery.rpc_timeout_seconds,
                         before_send=before_send)
        except Exception:
            observation = "not_sent" if rejected is not None else "unknown"
        try:
            self.state.observe_attempt(run_id, key, observation)
        except Exception:
            # The pre-send marker is authoritative; this observation is optional.
            return rejected or SteeringResult("attempted", "observation_unavailable", key, target=target)
        return rejected or SteeringResult("attempted", observation, key, target=target)
