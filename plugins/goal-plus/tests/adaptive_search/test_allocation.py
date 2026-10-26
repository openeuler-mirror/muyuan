from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus.application.allocation import AllocationDecisions
from goal_plus.application.allocation_apply import AllocationApply
from goal_plus.domain.adaptive.models import (
    AllocationExpansionIntent,
    AllocationPlan,
    ExpansionSource,
    SearchSnapshot,
    SnapshotCandidate,
    SnapshotNode,
    snapshot_identity,
)
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import ResultLedgerEntry
from goal_plus.domain.search import AgentSessionRecord, CandidateTask, RunState
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.ports.state import CandidateRecord, RunRecord


class Clock:
    def timestamp(self) -> str:
        return "2026-09-17T00:00:00Z"


def artifact(identity: str) -> ArtifactRef:
    return ArtifactRef(kind="git_commit", id=identity, provider="git_worktree")


def source(candidate_id: str, utility: float) -> tuple[ExpansionSource, SnapshotNode]:
    exact = ExpansionSource(
        candidate_id=candidate_id,
        iteration=1,
        settlement_id=f"settlement_{candidate_id}",
        node_id=f"{candidate_id}/iteration-0001",
        verifier_record_id=f"record_{candidate_id}",
        attempt_artifact_ref=artifact(f"attempt_{candidate_id}"),
        attempt_artifact_hash=f"hash_{candidate_id}",
        ledger_artifact_ref=artifact(f"ledger_{candidate_id}"),
    )
    return exact, SnapshotNode(
        candidate_id=candidate_id,
        iteration=1,
        node_id=exact.node_id,
        edge_kind="initial",
        settlement_id=exact.settlement_id,
        verifier_record_id=exact.verifier_record_id,
        verifier_subject_hash=("a" if candidate_id == "c001" else "b") * 64,
        source=exact,
        quality_score=utility,
        allocation_scores={
            "progress_reward": utility,
            "allocation_utility": utility,
        },
    )


def snapshot_and_plan() -> tuple[SearchSnapshot, AllocationPlan]:
    first_source, first_node = source("c001", 0.1)
    second_source, second_node = source("c002", 0.9)
    body = {
        "schema_version": 6,
        "run_id": "run_test",
        "frozen_spec_id": "frozen_test",
        "policy_id": "swe_search_uct/v1",
        "max_parallel": 2,
        "verifier_cutoff_sequence": 2,
        "candidates": [
            SnapshotCandidate(
                candidate_id="c001",
                allocation_depth=0,
                tip_node_id=first_node.node_id,
                nodes=[first_node],
            ).model_dump(mode="json"),
            SnapshotCandidate(
                candidate_id="c002",
                allocation_depth=0,
                tip_node_id=second_node.node_id,
                nodes=[second_node],
            ).model_dump(mode="json"),
        ],
        "verifier_record_ids": ["record_c001", "record_c002"],
    }
    revision, input_hash = snapshot_identity(body)
    snapshot = SearchSnapshot(
        **body, snapshot_revision=revision, input_hash=input_hash
    )
    intents = [
        AllocationExpansionIntent(
            source_candidate_id=item.candidate_id,
            source_iteration=item.iteration,
            source_settlement_id=item.settlement_id,
            source_node_id=item.node_id,
            source_verifier_record_id=item.verifier_record_id,
            eligible_retirements=["c001"],
            direction_key=f"expand-{item.candidate_id}",
            rationale=f"ranked source {index}",
        )
        for index, item in enumerate((second_source, first_source), 1)
    ]
    plan_body = {
        "schema_version": 5,
        "based_on_snapshot_revision": revision,
        "policy_id": "swe_search_uct/v1",
        "retire_candidate_ids": ["c001"],
        "expansions": [item.model_dump(mode="json") for item in intents],
        "reason_codes": ["replace-low-utility"],
    }
    plan = AllocationPlan(
        **plan_body, plan_hash=sha256_text(canonical_json(plan_body))
    )
    return snapshot, plan


def candidate(candidate_id: str, tmp_path: Path) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=candidate_id,
        status="evaluated",
        task=CandidateTask(
            run_id="run_test",
            candidate_id=candidate_id,
            plan_id="plan_test",
            hypothesis="candidate",
            workspace=tmp_path / candidate_id,
            workspace_provider="git_worktree",
            allowed_files=["main.py"],
            denied_files=["verify.py"],
            strategy_metadata={"slot": 1},
        ),
        results_ledger=[
            ResultLedgerEntry(
                source_run_id="run_test",
                source_candidate_id=candidate_id,
                iteration=1,
                artifact_ref=artifact(f"attempt_{candidate_id}"),
                ledger_artifact_ref=artifact(f"ledger_{candidate_id}"),
                metric_name="score",
                score=0.5,
                status="pass",
            )
        ],
    )


class State:
    def __init__(self, tmp_path: Path, decision) -> None:
        self.root_dir = tmp_path
        self.decision = decision
        self.run = RunRecord(
            run_id="run_test",
            frozen_spec_id="frozen_test",
            state=RunState.RUNNING,
            source_path="project",
            created_at="2026-09-17T00:00:00Z",

            next_candidate_index=3,
        )
        self.frozen = SimpleNamespace(
            spec=SimpleNamespace(
                budget=SimpleNamespace(max_parallel=2, max_candidates=4)
            )
        )
        self.candidates = {
            "c001": candidate("c001", tmp_path),
            "c002": candidate("c002", tmp_path),
        }
        self.sessions: list[AgentSessionRecord] = []
        self.lock_depth = 0
        self.materialization_projection = None

    @contextmanager
    def lock_run(self, run_id):
        assert run_id == "run_test"
        assert self.lock_depth == 0
        self.lock_depth = 1
        try:
            yield
        finally:
            self.lock_depth = 0

    def read_run(self, run_id):
        assert run_id == "run_test"
        return self.run

    def read_frozen_spec(self, frozen_spec_id):
        assert frozen_spec_id == "frozen_test"
        return self.frozen

    def load_allocation_decision(self, run_id, decision_id):
        assert (run_id, decision_id) == ("run_test", self.decision.decision_id)
        return self.decision

    def load_allocation_decisions(self, run_id):
        return [self.decision]

    def write_allocation_decision(self, decision):
        assert self.lock_depth == 1
        self.decision = decision.model_copy(deep=True)

    def read_candidate(self, run_id, candidate_id):
        return self.candidates[candidate_id]

    def find_candidate(self, run_id, candidate_id):
        return self.candidates.get(candidate_id)

    def read_candidates(self, run_id):
        return list(self.candidates.values())

    def write_retirement(self, run_id, candidate_id, decision_id):
        current = self.candidates[candidate_id]
        self.candidates[candidate_id] = current.model_copy(
            update={
                "allocation_eligibility": "retired",
                "retired_by_decision_id": decision_id,
            }
        )

    def write_candidate_index(self, run_id, next_candidate_index):
        self.run.next_candidate_index = next_candidate_index

    def read_plan(self, run_id, plan_id):
        return SimpleNamespace(plan_id=plan_id)

    def read_source_ledger(self, source_record):
        return source_record.results_ledger

    def read_agent_sessions(self, run_id):
        return list(self.sessions)

    def write_materialization_projection(self, run_id, projection):
        self.materialization_projection = projection


class Authorization:
    def require_search_execution(self, run_id):
        assert run_id == "run_test"


def executor(tmp_path: Path):
    snapshot, plan = snapshot_and_plan()

    class DecisionState:
        decisions = []

        def load_allocation_decisions(self, run_id):
            return list(self.decisions)

        def write_allocation_decision(self, decision):
            self.decisions = [decision]

    decision = AllocationDecisions(
        state=DecisionState(), clock=Clock()
    ).record(snapshot, plan)
    state = State(tmp_path, decision)
    events = []

    def default_materialize(request):
        events.append(("materialize", request.candidate_id, request.expansion_source.candidate_id))
        original = candidate(request.candidate_id, tmp_path)
        child = original.model_copy(
            update={
                "status": "created",
                "task": original.task.model_copy(
                    update={
                        "proposal": request.proposal,
                        "expansion_source": request.expansion_source,
                    }
                ),
            }
        )
        state.candidates[child.candidate_id] = child
        return child

    def start_session(run_id, candidate_id, directive=None, *, worker_budget=None, run_locked=False):
        assert run_locked and state.lock_depth == 1
        existing = next((item for item in state.sessions if item.candidate_id == candidate_id), None)
        if existing is not None:
            return existing
        session = AgentSessionRecord(
            agent_session_id=f"agent_{candidate_id}",
            run_id=run_id,
            candidate_id=candidate_id,
            created_at=Clock().timestamp(),
            updated_at=Clock().timestamp(),
            directive=directive,
            workspace=tmp_path / candidate_id,
        )
        state.sessions.append(session)
        return session

    def dispatch(session, applied_decision):
        assert state.lock_depth == 1
        events.append(("dispatch", session.candidate_id, applied_decision.decision_id))

    application = AllocationApply(
        state=state,
        authorization=Authorization(),
        materialize=default_materialize,
        start_session=start_session,
        clock=Clock(),
    )
    return application, state, events, default_materialize, dispatch


def test_decision_record_preserves_ranked_exact_sources() -> None:
    snapshot, plan = snapshot_and_plan()

    class DecisionState:
        decisions = []

        def load_allocation_decisions(self, run_id):
            return self.decisions

        def write_allocation_decision(self, decision):
            self.decisions = [decision]

    decisions = AllocationDecisions(state=DecisionState(), clock=Clock())
    first = decisions.record(snapshot, plan)
    replay = decisions.record(snapshot, plan)

    assert replay == first
    assert [item.source.candidate_id for item in first.expansions] == ["c002", "c001"]
    assert all(item.new_candidate_id is None for item in first.expansions)


def test_executor_cancels_materializes_and_dispatches_once(tmp_path: Path) -> None:
    application, state, events, _materialize, dispatch = executor(tmp_path)

    def cancel(decision):
        assert state.lock_depth == 1
        assert state.candidates["c001"].allocation_eligibility == "eligible"
        events.append(("cancel", decision.decision_id))

    applied = application.apply(
        "run_test", state.decision.decision_id, cancel=cancel, dispatch=dispatch
    )
    replay = application.apply(
        "run_test", state.decision.decision_id, cancel=cancel, dispatch=dispatch
    )

    assert applied == replay
    assert applied.decision.status == "applied"
    assert state.candidates["c001"].allocation_eligibility == "retired"
    assert [event[0] for event in events] == ["cancel", "materialize", "dispatch"]
    assert len(applied.candidate_tasks) == len(applied.agent_sessions) == 1


def test_executor_uses_next_ranked_source_after_clean_failure(tmp_path: Path) -> None:
    application, state, events, default_materialize, dispatch = executor(tmp_path)

    def materialize(request):
        if request.expansion_source.candidate_id == "c002":
            events.append(("failed", "c002"))
            raise OSError("source unavailable")
        return default_materialize(request)

    application.materialize = materialize
    applied = application.apply(
        "run_test", state.decision.decision_id, cancel=lambda _decision: None, dispatch=dispatch
    )

    assert applied.decision.status == "applied"
    assert [item.status for item in applied.decision.expansions] == ["failed", "applied"]
    assert events[0] == ("failed", "c002")


def test_dispatch_failure_does_not_create_a_backup_child(tmp_path: Path) -> None:
    application, state, _events, _materialize, _dispatch = executor(tmp_path)

    def uncertain_dispatch(_session, _decision):
        raise TimeoutError("unknown dispatch")

    interrupted = application.apply(
        "run_test",
        state.decision.decision_id,
        cancel=lambda _decision: None,
        dispatch=uncertain_dispatch,
    )

    assert interrupted.decision.status == "interrupted"
    assert interrupted.decision.expansions[0].new_candidate_id in state.candidates
    assert interrupted.decision.expansions[1].new_candidate_id is None
    assert len(state.sessions) == 1
    first_id = interrupted.decision.expansions[0].new_candidate_id
    recovered = application.apply(
        "run_test", state.decision.decision_id,
        cancel=lambda _: pytest.fail("retirement must not repeat"), dispatch=_dispatch,
    )
    assert recovered.decision.status == "applied"
    assert recovered.decision.expansions[0].new_candidate_id == first_id
    assert recovered.decision.expansions[1].new_candidate_id is None
    assert len(state.sessions) == 1


def test_executor_selects_next_eligible_retirement_without_rescoring(tmp_path):
    app, state, _, _, dispatch = executor(tmp_path)
    state.decision.retire_candidate_ids = ["c001", "c002"]
    state.decision.expansions[0].eligible_retirements = ["c001"]
    state.decision.expansions[1].eligible_retirements = ["c002"]
    state.write_retirement("run_test", "c001", "another-decision")
    cancellations = []
    result = app.apply("run_test", state.decision.decision_id,
                       cancel=lambda d: cancellations.append(d.selected_retirement), dispatch=dispatch)
    assert result.decision.status == "applied"
    assert cancellations == ["c002"]
    assert [e.status for e in result.decision.expansions] == ["skipped", "applied"]
    assert state.candidates["c001"].retired_by_decision_id == "another-decision"


def test_executor_honors_persisted_selected_retirement(tmp_path):
    app, state, _, _, dispatch = executor(tmp_path)
    state.decision.retire_candidate_ids = ["c001", "c002"]
    state.decision.selected_retirement = "c002"
    state.decision.expansions[0].eligible_retirements = ["c001"]
    state.decision.expansions[1].eligible_retirements = ["c002"]
    cancellations = []

    result = app.apply(
        "run_test",
        state.decision.decision_id,
        cancel=lambda decision: cancellations.append(decision.selected_retirement),
        dispatch=dispatch,
    )

    assert result.decision.status == "applied"
    assert cancellations == ["c002"]
    assert state.candidates["c001"].allocation_eligibility == "eligible"
    assert state.candidates["c002"].retired_by_decision_id == state.decision.decision_id
    assert [expansion.status for expansion in result.decision.expansions] == [
        "skipped",
        "applied",
    ]


def test_capacity_exhausted_never_retires_candidate(tmp_path):
    app, state, _, _, _ = executor(tmp_path)
    state.frozen.spec.budget.max_candidates = 2
    result = app.apply("run_test", state.decision.decision_id,
                       cancel=lambda _: pytest.fail("capacity must precede retirement"),
                       dispatch=lambda *_: pytest.fail("must not dispatch"))
    assert result.decision.status == "superseded"
    assert all(c.allocation_eligibility == "eligible" for c in state.candidates.values())


def test_executor_recovers_pending_decisions_in_order_without_verifier(tmp_path):
    app, state, _, _, dispatched = executor(tmp_path)
    first = state.decision
    second = first.model_copy(deep=True, update={
        "decision_id": "allocation_" + "f" * 24, "verifier_sequence": first.verifier_sequence + 1})
    decisions = {d.decision_id: d for d in [second, first]}
    state.load_allocation_decisions = lambda _: list(decisions.values())
    state.load_allocation_decision = lambda _, identity: decisions[identity]

    def write(decision):
        assert state.lock_depth == 1
        decisions[decision.decision_id] = decision.model_copy(deep=True)

    state.write_allocation_decision = write
    def unknown(*_):
        raise TimeoutError("missing exact controller")

    result = app.apply("run_test", second.decision_id, cancel=lambda _: None, dispatch=unknown)
    assert result.decision.status == "pending"
    assert result.decision.selected_retirement is None
    assert decisions[first.decision_id].status == "interrupted"
    child_id = decisions[first.decision_id].expansions[0].new_candidate_id
    app.materialize = lambda _: pytest.fail("dispatch recovery must not repeat materialization")
    app.recover("run_test", cancel=lambda _: pytest.fail("already retired"), dispatch=dispatched)
    assert decisions[first.decision_id].status == "applied"
    assert decisions[first.decision_id].expansions[0].new_candidate_id == child_id
    assert decisions[second.decision_id].status == "superseded"


@pytest.mark.parametrize("status", ["pending", "interrupted"])
def test_admission_fences_only_the_selected_retirement(tmp_path, status):
    from goal_plus.application.adaptive_admission import AdaptiveAdmission
    _, state, _, _, _ = executor(tmp_path)
    state.frozen.spec.strategy = SimpleNamespace(adaptive_search_enabled=True)
    state.decision.retire_candidate_ids = ["c001", "c002"]
    state.decision.selected_retirement = "c001"
    state.decision.status = status
    guard = AdaptiveAdmission(state)
    with pytest.raises(RuntimeError, match="retires"):
        guard.assert_candidate_execution_allowed(state.candidates["c001"], "start_session")
    guard.assert_candidate_execution_allowed(state.candidates["c002"], "start_session")
    state.decision.status = "terminal_error"
    guard.assert_candidate_execution_allowed(state.candidates["c001"], "start_session")


def test_admission_does_not_fence_before_retirement_is_selected(tmp_path):
    from goal_plus.application.adaptive_admission import AdaptiveAdmission

    _, state, _, _, _ = executor(tmp_path)
    state.frozen.spec.strategy = SimpleNamespace(adaptive_search_enabled=True)
    state.decision.status = "pending"
    state.decision.selected_retirement = None
    guard = AdaptiveAdmission(state)

    for candidate in state.candidates.values():
        guard.assert_candidate_execution_allowed(candidate, "start_session")


@pytest.mark.parametrize("status", ["applied", "superseded", "terminal_error"])
def test_admission_does_not_fence_selected_retirement_after_decision_finishes(tmp_path, status):
    from goal_plus.application.adaptive_admission import AdaptiveAdmission

    _, state, _, _, _ = executor(tmp_path)
    state.frozen.spec.strategy = SimpleNamespace(adaptive_search_enabled=True)
    state.decision.selected_retirement = "c001"
    state.decision.status = status
    guard = AdaptiveAdmission(state)

    guard.assert_candidate_execution_allowed(state.candidates["c001"], "run_verifier")
    guard.assert_candidate_execution_allowed(state.candidates["c002"], "run_verifier")


def test_non_adaptive_admission_ignores_selected_retirement(tmp_path):
    from goal_plus.application.adaptive_admission import AdaptiveAdmission

    _, state, _, _, _ = executor(tmp_path)
    state.frozen.spec.strategy = SimpleNamespace(adaptive_search_enabled=False)
    state.decision.selected_retirement = "c001"
    state.decision.status = "pending"
    guard = AdaptiveAdmission(state)

    guard.assert_candidate_execution_allowed(state.candidates["c001"], "start_session")
    guard.assert_candidate_execution_allowed(state.candidates["c002"], "start_session")
