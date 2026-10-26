"""Freeze a local diagnostic view and pure result; never read peers or await messages."""

from goal_plus.domain.adaptive.models import steering_attempt_key
from goal_plus.domain.adaptive.policy import SteeringPolicySpec, evaluate_steering_policy
from goal_plus.domain.adaptive.search_diagnostic_guidance import (
    DiagnosticExperiment, ExperimentIdentity, SearchDiagnosticInput,
    DiagnosticEvaluation,
)
from goal_plus.domain.edit_surface import path_matches
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.verifier import (
    VerifierRecord, diagnostic_references, validate_diagnostics, validate_scores,
    verifier_subject_identity,
)
from goal_plus.ports.clock import Clock
from goal_plus.ports.steering import SteeringDiagnosticState, SteeringDiagnosticObservation, SteeringResult


def _experiment(receipt, verifier_hash: str) -> DiagnosticExperiment:
    # Metrics are committed hard-verifier facts. Only a full, explicit attestation qualifies.
    attestations = [values["steering_experiment"] for values in receipt.evidence.metrics.values()
                    if isinstance(values, dict) and "steering_experiment" in values]
    identity = None
    if len(attestations) == 1:
        try:
            identity = ExperimentIdentity.model_validate(attestations[0])
        except ValueError:
            pass  # Missing/malformed identity is unknown, never inferred from scores or commits.
    return DiagnosticExperiment(settlement_id=receipt.settlement_id,
                                verifier_contract_hash=verifier_hash, identity=identity)


class SteeringDiagnostics:
    def __init__(self, *, state: SteeringDiagnosticState, clock: Clock) -> None:
        self.state, self.clock = state, clock

    def evaluate(self, record: VerifierRecord, *, policy: SteeringPolicySpec,
                 renderer_version: str) -> tuple[SearchDiagnosticInput, DiagnosticEvaluation]:
        run_id, candidate_id = record.subject.run_id, record.subject.candidate_id
        key = steering_attempt_key(record.record_id, record.subject, policy.identity,
                                   policy.delivery, renderer_version)
        with self.state.lock_run(run_id):
            existing = self.state.read_diagnostic(run_id, key)
            if existing is not None:
                saved, _ = existing
                if (saved.source_record_id != record.record_id or saved.subject_hash != verifier_subject_identity(record.subject)
                        or saved.candidate_id != candidate_id or saved.score_contract_hash != record.score_contract_hash):
                    raise ValueError("saved diagnostic does not match its source")
                return existing
            candidate = self.state.read_candidate(run_id, candidate_id)
            frozen = self.state.read_frozen_spec(record.subject.frozen_spec_id)
            if frozen.spec.strategy.steering != policy:
                raise ValueError("steering policy must match the frozen run contract")
            source = next((row for row in candidate.iterations
                           if row.settlement_receipt == record.subject.settlement), None)
            if source is None or source.verifier_submission is None:
                raise ValueError("missing immutable diagnostic submission")
            submission = source.verifier_submission
            if (submission.subject != record.subject
                    or submission.score_contract.contract_hash != record.score_contract_hash):
                raise ValueError("diagnostic source contract mismatch")
            validate_scores(record.scores, submission.score_contract.score_rules)
            refs = diagnostic_references(submission.input, submission.subject)
            validate_diagnostics(record.steering_findings, record.scores, submission.score_contract, refs)
            if record.steering_findings is None or not set(policy.required_score_keys).issubset(record.scores or {}):
                raise ValueError("missing registered diagnostic scores/findings")
            receipt = record.subject.settlement
            # Findings may cite only candidate-visible inputs, never protected file contents.
            if (receipt.evidence.touched_denied_files or receipt.evidence.changed_outside_allowed
                    or any(not path_matches(path, candidate.task.allowed_files)
                           or path_matches(path, candidate.task.denied_files)
                           for path in receipt.evidence.attempt_changed_files)):
                refs.pop("diff", None)
            findings = record.steering_findings
            scope_ref = findings.hypothesis.scope_ref if findings.hypothesis else "hypothesis"
            # Conservative principal-claim identity across actions. Do not incorporate Record/session IDs.
            issue_key = sha256_text(canonical_json({
                "hypothesis": " ".join(submission.input.hypothesis.split()),
                "scope": " ".join(refs.get(scope_ref, "").split()),
            }))
            verifier_hash = sha256_text(canonical_json({
                "frozen_spec_id": frozen.frozen_spec_id,
                "process_verifiers": [v.model_dump(mode="json") for v in frozen.spec.process_verifiers],
            }))
            experiment = _experiment(receipt, verifier_hash)
            # The settled content digest is baseline-relative, not iteration-diff-relative.
            # Moving the diff base, ledger commits, relative disposition and model wording
            # are not new evidence. In particular keep -> retain is an unchanged retest.
            evidence_revision = sha256_text(canonical_json({
                "artifact_content": receipt.attempt_artifact_hash,
                "hard_outcome": {"process_passed": receipt.evidence.process_passed,
                                 "hard_score": receipt.evidence.hard_score},
                "experiment": experiment.identity.model_dump(mode="json") if experiment.identity else None,
            }))
            history = sorted((row.settlement_receipt for row in candidate.iterations
                              if row.settlement_receipt is not None and row.iteration < receipt.iteration),
                             key=lambda row: row.iteration)
            limit = policy.parameters.history_limit
            input = SearchDiagnosticInput(source_record_id=record.record_id,
                subject_hash=verifier_subject_identity(record.subject), candidate_id=candidate_id,
                score_contract_hash=record.score_contract_hash,
                scores={key: record.scores[key] for key in policy.required_score_keys},
                findings=findings, public_references=refs,
                experiment=experiment,
                experiment_history=tuple(_experiment(row, verifier_hash) for row in history[-limit:]),
                history_coverage="truncated" if len(history) > limit else "complete",
                source_generation=candidate.execution_generation,
                usage=self.state.list_attempts(run_id, candidate_id), observed_at=self.clock.now(),
                issue_key=issue_key, evidence_revision=evidence_revision)
            result = evaluate_steering_policy(input, policy=policy)
            self.state.write_diagnostic(run_id, key, input, result, mode=policy.delivery.mode)
            return input, result

    def observe(self, record: VerifierRecord, *, policy: SteeringPolicySpec,
                renderer_version: str, result: SteeringResult) -> None:
        # A replay/concurrent loser must not mask the original sender's outcome.
        if result.reason_code == "already_consumed":
            return
        key = steering_attempt_key(record.record_id, record.subject, policy.identity,
                                   policy.delivery, renderer_version)
        self.state.observe_diagnostic(record.subject.run_id, key,
            SteeringDiagnosticObservation(result=result, observed_at=self.clock.timestamp()))
