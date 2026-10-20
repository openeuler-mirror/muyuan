"""Candidate-owned evaluation versions; execution authority remains with its run."""

from goal_plus.application.candidate_execution import (
    require_candidate_not_recovering,
    require_no_pending_verification,
    require_worker_iteration_state,
)
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.search import CandidateSpecRevision, CandidateSpecRevisionInput, FrozenSpec, SearchSpec
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.ports.candidate_settlement import CandidateSettlementState
from goal_plus.ports.clock import Clock
from goal_plus.ports.search_bootstrap import FrozenSpecState
from goal_plus.ports.state import CandidateRecord, RunRecord


def candidate_spec_id(run: RunRecord, record: CandidateRecord) -> str:
    return record.active_frozen_spec_id or run.frozen_spec_id


def iteration_spec_id(run: RunRecord, iteration: IterationRecord) -> str:
    return iteration.frozen_spec_id or run.frozen_spec_id


class CandidateSpecRevisions:
    def __init__(self, *, state: CandidateSettlementState, specs: FrozenSpecState,
                 sessions: CandidateSessions, authorization: GoalAuthorization, clock: Clock) -> None:
        self.state, self.specs, self.sessions = state, specs, sessions
        self.authorization, self.clock = authorization, clock

    def revise(self, agent_session_id: str, request_id: str, parent_frozen_spec_id: str,
               revision_reason: str, revision: CandidateSpecRevisionInput) -> FrozenSpec:
        if not request_id.strip() or not parent_frozen_spec_id.strip() or not revision_reason.strip():
            raise ValueError("candidate revision requires request, parent and reason")
        session = self.sessions.session(agent_session_id)
        request_hash = sha256_text(canonical_json({
            "agent_session_id": agent_session_id, "parent_frozen_spec_id": parent_frozen_spec_id,
            "revision_reason": revision_reason, "revision": revision.model_dump(mode="json", exclude_none=True),
        }))
        # Match the verifier's lock order. Admission cannot race the interval
        # before a local immutable verification request has been persisted.
        with self.state.lock_verifier(session.run_id, session.candidate_id, blocking=False):
            with self.state.lock_run(session.run_id):
                run = self.state.read_run(session.run_id)
                self.authorization.require_search_execution(run.run_id)
                require_worker_iteration_state(run, "revise candidate spec")
                self.sessions.session(agent_session_id, run_id=run.run_id)
                record = self.state.read_candidate(run.run_id, session.candidate_id)
                prior = next((item for item in record.spec_revisions if item.request_id == request_id), None)
                if prior is not None:
                    if prior.request_hash != request_hash:
                        raise ValueError("CandidateSpecRequestConflict: revision request content changed")
                    return self.specs.read(prior.frozen_spec_id)
                require_candidate_not_recovering(record)
                require_no_pending_verification(record)
                if record.accepted_attempt is not None:
                    raise RuntimeError("VerifierInProgress: an accepted attempt fixes the execution spec")
                if candidate_spec_id(run, record) != parent_frozen_spec_id:
                    raise RuntimeError("CandidateSpecParentMismatch: revision parent is no longer active")
                parent = self.specs.read(parent_frozen_spec_id)
                if parent.spec.strategy.adaptive_search_enabled and not parent.spec.strategy.fork_enabled:
                    raise ValueError("candidate spec revision requires fixed fresh or fork context")
                if record.active_spec_hash is not None and record.active_spec_hash != parent.spec_hash:
                    raise RuntimeError("CandidateSpecBindingMismatch: active spec hash changed")
                payload = parent.spec.model_dump(mode="json")
                changes = revision.model_dump(mode="json", exclude_none=True)
                selection = changes.pop("selection", None)
                payload.update(changes)
                if selection is not None:
                    payload["strategy"]["selection"] = selection
                spec = SearchSpec.model_validate(payload)
                if spec.strategy.model_dump(exclude={"selection"}) != parent.spec.strategy.model_dump(exclude={"selection"}):
                    raise ValueError("CandidateSpecCapabilityMismatch: revision cannot change frozen evaluator capabilities")
                if spec == parent.spec:
                    raise ValueError("candidate spec revision must change its evaluation contract")
                binding = record.task.candidate_origin
                group_id = binding.dispatch_group_id if binding is not None else None
                branch_index = binding.dispatch_target_index if binding is not None else None
                spec_hash = sha256_text(canonical_json({
                    "parent_spec_hash": parent.spec_hash, "spec": spec.model_dump(mode="json"),
                    "revision_reason": revision_reason, "run_id": run.run_id,
                    "candidate_id": record.candidate_id,
                    "dispatch_group_id": group_id, "dispatch_target_index": branch_index,
                }))
                created_at = self.clock.timestamp()
                frozen = parent.model_copy(deep=True, update={
                    "frozen_spec_id": f"spec_{spec_hash[:12]}", "spec_hash": spec_hash, "spec": spec,
                    "parent_frozen_spec_id": parent.frozen_spec_id, "revision_reason": revision_reason,
                    "dispatch_group_id": group_id, "dispatch_target_index": branch_index,
                    # The original baseline was evaluated under the old contract.
                    "origin_verification": None, "created_at": created_at,
                })
                self.specs.write(frozen)
                frozen = self.specs.read(frozen.frozen_spec_id)
                record.spec_revisions.append(CandidateSpecRevision(
                    request_id=request_id, request_hash=request_hash,
                    parent_frozen_spec_id=parent.frozen_spec_id, frozen_spec_id=frozen.frozen_spec_id,
                    spec_hash=frozen.spec_hash, revision_reason=revision_reason, created_at=created_at,
                ))
                record.active_frozen_spec_id, record.active_spec_hash = frozen.frozen_spec_id, frozen.spec_hash
                if record.results_ledger_metric_name is None:
                    record.results_ledger_metric_name = self.specs.read(run.frozen_spec_id).spec.metric_name
                record.task.acceptance = list(spec.candidate_acceptance)
                record.task.strategy_metadata.update({"frozen_spec_id": frozen.frozen_spec_id, "spec_hash": frozen.spec_hash})
                record.promotion_report = record.promotion_evidence = None
                self.state.write_candidate(run.run_id, record)
                return frozen
