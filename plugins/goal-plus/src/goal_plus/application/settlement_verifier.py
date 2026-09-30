"""Settlement's single-iteration delivery to the Verifier queue."""

import math

from goal_plus.domain.verifier import VerifierInput, VerifierSubmission, VerifierSubject, verifier_score_contract
from goal_plus.ports.artifacts import ArtifactComparison


def verifier_subject(frozen, record, iteration):
    receipt = iteration.settlement_receipt
    if receipt is None or receipt.run_id != record.task.run_id or receipt.candidate_id != record.candidate_id:
        raise ValueError("verifier requires an exact settlement receipt")
    return VerifierSubject(frozen_spec_id=frozen.frozen_spec_id, settlement=receipt)


class SettlementVerifier:
    def __init__(self, *, workflow, artifacts, source) -> None:
        self.workflow, self.artifacts, self.source = workflow, artifacts, source

    def prepare(self, frozen, record, iteration):
        if not frozen.spec.strategy.requires_llm_verifier:
            return None
        subject = verifier_subject(frozen, record, iteration)
        verifier = frozen.spec.strategy.verifier
        contract = verifier_score_contract(verifier.scores if verifier else ())
        receipt = subject.settlement
        diff, error = "", None
        base = receipt.attempt_base_artifact_ref
        try:
            if base is None and any(rule.requires_comparison_base for rule in contract.score_rules):
                raise ValueError("selected verifier scores require an exact comparison base")
            if base is not None and base != receipt.attempt_artifact_ref:
                diff = self.artifacts.diff(ArtifactComparison(workspace=record.task.workspace,
                    base=base, head=receipt.attempt_artifact_ref,
                    source_relative_path=record.task.workspace_source_subdir),
                    paths=list(receipt.evidence.attempt_changed_files), max_bytes=512 * 1024,
                    function_context=True).text
        except Exception as exc:
            error = f"settlement diff unavailable: {exc}"
        parent = record.task.expansion_source
        threshold = frozen.spec.constraints.get("success_threshold")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not math.isfinite(threshold):
            threshold = None
        submission = VerifierSubmission(subject=subject, score_contract=contract,
            input=VerifierInput(objective=frozen.spec.objective,
                candidate_acceptance=frozen.spec.candidate_acceptance,
                metric_direction=frozen.spec.metric_direction, success_threshold=threshold,
                hypothesis=iteration.hypothesis or "", artifact_diff=diff,
                agent_harness=frozen.agent_harness, initial_artifact_ref=record.initial_attempt_base_artifact_ref,
                parent_node_id=parent.node_id if parent else None, allocation_depth=record.task.allocation_depth,
                replaces_candidate_id=record.task.proposal.metadata.get("replaces_candidate_id") if record.task.proposal else None,
                input_error=error))
        iteration.verifier_submission = submission
        return submission

    def submit(self, submission):
        if submission is not None:
            try:
                return self.workflow.ensure_assessment(
                    submission.subject, submission.score_contract, submission.input)
            except OSError:
                # The committed submission remains recoverable without rebuilding its input.
                return None

    def replay(self, run_id: str):
        # Replay committed outbound inputs after a crash between settlement and enqueue.
        # No inference, re-assessment or historical context is constructed here.
        pending = [i.verifier_submission for c in self.source.read_candidates(run_id)
                   for i in c.iterations if i.verifier_submission is not None]
        ordered = sorted(
            pending,
            key=lambda s: (
                s.subject.settlement.created_at,
                s.subject.candidate_id,
                s.subject.iteration,
            ),
        )
        return self.workflow.ensure_assessments(
            [(item.subject, item.score_contract, item.input) for item in ordered]
        )
