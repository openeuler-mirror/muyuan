"""Settlement's single-iteration delivery to the Verifier queue."""

import math

from goal_plus.domain.search import FrozenSpec, CandidateOrigin
from goal_plus.domain.settlement import SettlementReceipt
from goal_plus.domain.verifier import DispatchAuditContext, VerifierInput, VerifierSubmission, VerifierSubject, verifier_score_contract
from goal_plus.ports.artifacts import ArtifactComparison
from goal_plus.domain.serialization import canonical_json, sha256_text


def verifier_subject(frozen, record, iteration):
    receipt = iteration.settlement_receipt
    if receipt is None or receipt.run_id != record.task.run_id or receipt.candidate_id != record.candidate_id:
        raise ValueError("verifier requires an exact settlement receipt")
    return VerifierSubject(frozen_spec_id=frozen.frozen_spec_id, settlement=receipt)


def build_dispatch_audit_context(
    frozen: FrozenSpec,
    binding: CandidateOrigin | None,
    receipt: SettlementReceipt,
) -> DispatchAuditContext:
    """Project immutable fork provenance and the exact settled artifact pair."""
    if binding is None or not binding.provider_session_id:
        raise ValueError("ForkAuditBindingMissing: settled fork branch has no native identity")
    revised = binding.frozen_spec_id != frozen.frozen_spec_id
    if ((not revised and binding.spec_hash != frozen.spec_hash)
            or (revised and (frozen.dispatch_group_id, frozen.dispatch_target_index)
                != (binding.dispatch_group_id, binding.dispatch_target_index))
            or (receipt.frozen_spec_id is not None and receipt.frozen_spec_id != frozen.frozen_spec_id)
            or (receipt.spec_hash is not None and receipt.spec_hash != frozen.spec_hash)):
        raise ValueError("ForkAuditBindingMismatch: settlement must retain its execution spec")
    refs = [receipt.attempt_artifact_ref]
    if receipt.attempt_base_artifact_ref is not None:
        refs.append(receipt.attempt_base_artifact_ref)
    attachment = binding.workspace_attachment
    return DispatchAuditContext(
        runtime_provider=binding.runtime_provider,
        main_session_id=binding.main_session_id,
        checkpoint_id=binding.checkpoint_id,
        checkpoint_kind=binding.checkpoint_kind,
        parent_native_session_id=binding.parent_native_session_id,
        provider_session_id=binding.provider_session_id,
        workspace=binding.workspace,
        workspace_hash=(
            sha256_text(canonical_json(attachment.model_dump(mode="json")))
            if attachment is not None else sha256_text(str(binding.workspace))
        ),
        workspace_attachment=attachment,
        frozen_spec_id=frozen.frozen_spec_id,
        spec_hash=frozen.spec_hash,
        initial_frozen_spec_id=binding.frozen_spec_id if revised else None,
        initial_spec_hash=binding.spec_hash if revised else None,
        parent_frozen_spec_id=frozen.parent_frozen_spec_id,
        artifact_refs=tuple(refs),
        iteration=receipt.iteration,
        settlement_id=receipt.settlement_id,
        launch_receipt=binding.launch_receipt,
        context_projection_digest=binding.context_projection_digest,
    )


class SettlementVerifier:
    def __init__(self, *, workflow, artifacts, source, knowledge_snapshot=None) -> None:
        self.workflow, self.artifacts, self.source = workflow, artifacts, source
        # Optional shared-cache projection provider: (frozen, run_id) -> dict.
        # Injected only when the run has a cache; keeps the no-cache path free
        # of cache dependencies.
        self.knowledge_snapshot = knowledge_snapshot

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
        knowledge_snapshot = None
        if (
            self.knowledge_snapshot is not None
            and frozen.spec.shared_knowledge.enabled
            and frozen.spec.shared_knowledge.verifier_input
        ):
            knowledge_snapshot = self.knowledge_snapshot(frozen, record.task.run_id)
        parent = record.task.expansion_source
        threshold = frozen.spec.constraints.get("success_threshold")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not math.isfinite(threshold):
            threshold = None
        dispatch_audit_context = None
        if frozen.spec.strategy.fork_enabled and frozen.spec.strategy.llm_validation != "off":
            dispatch_audit_context = build_dispatch_audit_context(frozen, record.task.candidate_origin, receipt)
        submission = VerifierSubmission(subject=subject, score_contract=contract,
            input=VerifierInput(objective=frozen.spec.objective,
                candidate_acceptance=frozen.spec.candidate_acceptance,
                metric_direction=frozen.spec.metric_direction, success_threshold=threshold,
                hypothesis=iteration.hypothesis or "", artifact_diff=diff,
                agent_harness=frozen.agent_harness, initial_artifact_ref=record.initial_attempt_base_artifact_ref,
                parent_node_id=parent.node_id if parent else None, allocation_depth=record.task.allocation_depth,
                replaces_candidate_id=record.task.proposal.metadata.get("replaces_candidate_id") if record.task.proposal else None,
                input_error=error, knowledge_snapshot=knowledge_snapshot,
                dispatch_audit_context=dispatch_audit_context))
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
