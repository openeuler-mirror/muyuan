"""Adaptive branch routing and serial SteeringExecutor host delivery."""

from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from types import SimpleNamespace

import pytest

from goal_plus.adaptive.search import AdaptiveSearch
from goal_plus.adaptive.steering import Steering, SteeringExecutor
from goal_plus.application.steering import SteeringDiagnostics
from goal_plus.application.adaptive_snapshot import AdaptiveSnapshotBuilder
from goal_plus.application.candidate_queries import CandidateHostQueries
from goal_plus.domain.adaptive.models import (
    SteeringDecision, SteeringDeliverySpec, SteeringMessage, SteeringPolicyIdentity, SteeringTarget,
)
from goal_plus.domain.agents import AgentSessionHandle
from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.search import AgentSessionRecord, CandidateTask, RunState
from goal_plus.domain.time import timestamp_epoch
from goal_plus.domain.verifier import verifier_subject_identity
from goal_plus.infrastructure.state.steering import FileSteeringState
from goal_plus.ports.state import CandidateRecord, RunRecord
from goal_plus.ports.session import SessionCallSnapshot
from goal_plus.session_host import _SteeringTransport
from tests.adaptive_search.test_snapshot import State as SnapshotState, record
from tests.test_adaptive_search_flow import Allocation


class State(FileSteeringState):
    """Real file exclusion/attempt persistence with controlled authoritative reads."""

    def __init__(self, root, output):
        super().__init__(root)
        receipt = output.subject.settlement
        self.run = RunRecord(run_id=receipt.run_id, frozen_spec_id=output.subject.frozen_spec_id,
            state=RunState.RUNNING, source_path="project", created_at=receipt.created_at)
        self.candidate = CandidateRecord(candidate_id=receipt.candidate_id, status="evaluated",
            task=CandidateTask(run_id=receipt.run_id, candidate_id=receipt.candidate_id,
                hypothesis="test", workspace=root / "candidate", workspace_provider="git_worktree",
                allowed_files=["main.py"], denied_files=["verify.py"]),
            results_ledger_artifact_ref=receipt.ledger_artifact_ref,
            iterations=[IterationRecord(iteration=receipt.iteration, settlement_id=receipt.settlement_id,
                agent_session_id="agent_1", settlement_receipt=receipt,
                artifact_ref=receipt.attempt_artifact_ref, artifact_hash=receipt.attempt_artifact_hash,
                attempt_base_artifact_ref=receipt.attempt_base_artifact_ref,
                score=receipt.evidence.hard_score, process_passed=receipt.evidence.process_passed,
                disposition=receipt.disposition, workspace_artifact_after_settlement=receipt.ledger_artifact_ref,
                created_at=receipt.created_at)])
        self.session = AgentSessionRecord(agent_session_id="agent_1", run_id=receipt.run_id,
            candidate_id=receipt.candidate_id, workspace=root / "candidate",
            session_handle=AgentSessionHandle(external_id="native_1"),
            created_at=receipt.created_at, updated_at=receipt.created_at)
        self.frozen = SimpleNamespace(agent_harness="codex", runtime_provider="direct",
            spec=SimpleNamespace(strategy=SimpleNamespace(orchestration_mode="adaptive_search")))
        self.decisions = []

    def read_run(self, run_id):
        return self.run

    def read_frozen_spec(self, frozen_id):
        return self.frozen

    def read_candidate(self, run_id, candidate_id):
        return self.candidate

    def read_agent_session(self, session_id, run_id=None):
        assert session_id == "agent_1"
        return self.session

    def load_allocation_decisions(self, run_id):
        return self.decisions


class Host:
    def __init__(self, state):
        self.state, self.calls = state, []
        self.available = True
        self.on_send = lambda: None
        self.on_resolve = lambda: None
        self.before_send = lambda: None

    def active_call(self, session):
        self.on_resolve()
        if not self.available:
            return None
        return SteeringTarget(agent_session_id=session.agent_session_id,
            execution_generation=session.execution_generation, native_session_id="native_1", call_id="call_1")

    def message(self, target, text, *, timeout, before_send):
        self.before_send()
        before_send()
        # A native callback must not inherit run.lock.
        from goal_plus.infrastructure.state.files import exclusive_file_lock
        with exclusive_file_lock(self.state.run_dir("run_1") / "run.lock", blocking=False):
            self.calls.append((target, text, timeout))
            self.on_send()


@pytest.fixture
def setup(tmp_path):
    output = record(1)
    state = State(tmp_path, output)
    clock = SimpleNamespace(now=lambda: timestamp_epoch(output.subject.settlement.created_at) + 1,
                            timestamp=lambda: "2026-01-01T00:00:01Z")
    authorization = SimpleNamespace(require_search_execution=lambda _: None)
    context = SimpleNamespace(outer_deadline=lambda _: None)
    queries = CandidateHostQueries(state, context)
    executor = SteeringExecutor(state=state, authorization=authorization, queries=queries, clock=clock)
    identity = SteeringPolicyIdentity(policy="fixture/v1", parameters_hash="a" * 64)
    delivery = SteeringDeliverySpec(rpc_timeout_seconds=1, max_source_age_seconds=60)
    decision = SteeringDecision(source_record_id=output.record_id,
        subject_hash=verifier_subject_identity(output.subject), candidate_id=output.subject.candidate_id,
        message=SteeringMessage(text="Check the evidence for your current hypothesis."),
        reason_code="fixture")
    component = Steering(executor=executor, diagnostics=SteeringDiagnostics(state=state, clock=clock))
    return SimpleNamespace(output=output, state=state, executor=executor, identity=identity,
        decision=decision, delivery=delivery, component=component, host=Host(state), clock=clock,
        authorization=authorization, context=context)


def accept(h):
    return h.executor.steer(h.output, h.decision, policy=h.identity, delivery=h.delivery,
        renderer_version="fixture/v1", host=h.host)


def test_executor_delivery_is_once_across_payload_changes_and_new_executor(setup):
    h = setup
    before = h.output.model_dump()
    first = accept(h)
    assert first.status == "attempted"
    assert first.reason_code == "returned"
    saved = h.state.read_attempt("run_1", first.attempt_key)
    assert saved.message.text == h.host.calls[0][1]
    assert saved.observation == "returned"
    h.decision = h.decision.model_copy(update={"message": SteeringMessage(
        text="Different text must not create another send permission.")})
    assert accept(h).reason_code == "already_consumed"
    reloaded = FileSteeringState(h.state.root_dir)
    assert reloaded.read_attempt("run_1", first.attempt_key) == saved
    h.executor.state = reloaded
    assert accept(h).reason_code == "already_consumed"
    assert len(h.host.calls) == 1
    assert h.output.model_dump() == before


def test_same_candidate_can_receive_multiple_guidance_attempts(setup):
    first = accept(setup)
    second_identity = setup.identity.model_copy(update={"parameters_hash": "b" * 64})
    second_decision = setup.decision.model_copy(update={
        "message": SteeringMessage(text="Review the next verifier result immediately."),
    })
    second = setup.executor.steer(
        setup.output,
        second_decision,
        policy=second_identity,
        delivery=setup.delivery,
        renderer_version="fixture/v1",
        host=setup.host,
    )
    assert first.status == second.status == "attempted"
    assert first.attempt_key != second.attempt_key
    assert [call[1] for call in setup.host.calls] == [
        setup.decision.message.text,
        second_decision.message.text,
    ]


@pytest.mark.parametrize("failure", [TimeoutError, PermissionError, RuntimeError])
def test_failed_send_is_not_retried(setup, failure):
    h = setup
    def fail():
        raise failure("no receipt")
    h.host.on_send = fail
    assert accept(h).reason_code == "unknown"
    assert accept(h).reason_code == "already_consumed"
    assert len(h.host.calls) == 1


def test_crash_after_consumption_can_lose_message_but_cannot_resend(setup, monkeypatch):
    h = setup
    consume = h.state.consume_attempt
    def crash(attempt):
        consume(attempt)
        raise KeyboardInterrupt("process lost before native send")
    monkeypatch.setattr(h.state, "consume_attempt", crash)
    with pytest.raises(KeyboardInterrupt):
        accept(h)
    monkeypatch.setattr(h.state, "consume_attempt", consume)
    assert accept(h).reason_code == "already_consumed"
    assert h.host.calls == []


def test_crash_after_send_and_missing_observation_do_not_resend(setup, monkeypatch):
    h = setup
    def crash(*_):
        raise KeyboardInterrupt("lost before consumer delivery confirmation")
    monkeypatch.setattr(h.state, "observe_attempt", crash)
    with pytest.raises(KeyboardInterrupt):
        accept(h)
    assert accept(h).reason_code == "already_consumed"
    assert len(h.host.calls) == 1


@pytest.mark.parametrize("method", ["consume_attempt", "observe_attempt"])
def test_storage_errors_do_not_escape_to_verifier(setup, monkeypatch, method):
    h = setup
    def fail(*_):
        raise OSError("storage unavailable")
    monkeypatch.setattr(h.state, method, fail)
    result = accept(h)
    assert result.status == ("error" if method == "consume_attempt" else "attempted")
    assert len(h.host.calls) == (0 if method == "consume_attempt" else 1)


def test_concurrent_consumers_have_one_send_permission(setup):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: accept(setup), range(4)))
    assert sum(r.status == "attempted" for r in results) == 1
    assert len(setup.host.calls) == 1


@pytest.mark.parametrize("gate", ["retired", "pending_retirement", "stale_generation", "late_record",
                                   "no_provenance", "no_call", "deadline", "paused", "expired",
                                   "run_finished", "retirement_race", "rollback"])
def test_source_and_execution_gates_never_send(setup, gate):
    h = setup
    if gate == "retired":
        h.state.candidate.allocation_eligibility = "retired"
    elif gate in {"pending_retirement", "retirement_race"}:
        def fence():
            h.state.decisions.append(SimpleNamespace(status="pending", selected_retirement="c001",
                                                     decision_id="allocation_old"))
        if gate == "retirement_race":
            h.host.on_resolve = fence
        else:
            fence()
    elif gate == "stale_generation":
        h.state.candidate.execution_generation += 1
    elif gate == "late_record":
        h.state.candidate.iterations.append(h.state.candidate.iterations[-1].model_copy(update={"iteration": 2}))
    elif gate == "no_provenance":
        h.state.candidate.iterations[-1].agent_session_id = None
    elif gate == "no_call":
        h.host.available = False
    elif gate == "deadline":
        h.context.outer_deadline = lambda _: "2025-01-01T00:00:00Z"
    elif gate == "paused":
        def denied(_):
            raise PermissionError("paused")
        h.authorization.require_search_execution = denied
    elif gate == "expired":
        h.clock.now = lambda: timestamp_epoch(h.output.subject.settlement.created_at) + 61
    elif gate == "run_finished":
        h.state.run.state = RunState.FAILED
    elif gate == "rollback":
        h.output = record(2, rollback_to=1)
    accept(h)
    assert h.host.calls == []
    assert not list(h.state.run_dir("run_1").glob("steering-attempts/*.json"))


def test_wrong_target_and_source_decisions_are_refused(setup):
    h = setup
    h.decision = h.decision.model_copy(update={"candidate_id": "c002"})
    assert accept(h).reason_code == "source_mismatch"
    assert h.host.calls == []


@pytest.mark.parametrize("branch", ["none", "plan", "policy_error", "existing", "failed"])
def test_allocation_exclusively_selects_steering_branch(setup, monkeypatch, branch):
    h = setup
    snapshot_state = SnapshotState()
    snapshot_state.write_policy_error = lambda *_: None
    allocation = Allocation(SimpleNamespace(status="pending", run_id="run_1", decision_id="allocation_1"))
    if branch == "existing":
        allocation.list = lambda _: [SimpleNamespace(based_on_snapshot_revision=snapshot_state.snapshots[-1].snapshot_revision)]
    def evaluate(*_, **__):
        if branch == "policy_error":
            raise RuntimeError("allocation failed")
        return None if branch == "none" else object()
    monkeypatch.setattr("goal_plus.adaptive.search.evaluate_allocation_policy", evaluate)
    steering_calls = []
    steering = SimpleNamespace(accept=lambda output, **_: (steering_calls.append(output), accept(h))[1])
    adaptive = AdaptiveSearch(allocation=allocation,
        snapshots=AdaptiveSnapshotBuilder(state=snapshot_state), steering=steering)
    from goal_plus.domain.adaptive.policy import SweSearchUctPolicySpec
    def execute(*_):
        raise RuntimeError("execution failed; must not fall back to steering")
    adaptive.accept_verifier_record(record(1, failed=True) if branch == "failed" else h.output,
        policy=SweSearchUctPolicySpec(policy="swe_search_uct/v1"), max_parallel=2,
        execute=execute, steering_host=h.host)
    assert len(steering_calls) == (1 if branch == "none" else 0)
    assert len(h.host.calls) == (1 if branch == "none" else 0)


def test_steering_component_is_disabled_without_a_frozen_policy(setup):
    h = setup
    assert h.component.accept(h.output, host=h.host, policy=None).reason_code == "disabled"
    assert h.host.calls == []


def test_verifier_terminal_delivery_is_acknowledged_even_when_message_fails(setup):
    from goal_plus.llm_verifier import VerifierQueue
    from goal_plus.domain.adaptive.policy import SweSearchUctPolicySpec

    h = setup
    def unavailable():
        raise TimeoutError("no reply")
    h.host.on_send = unavailable
    steering = SimpleNamespace(accept=lambda output, **_: accept(h))
    adaptive = AdaptiveSearch(allocation=Allocation(),
        snapshots=AdaptiveSnapshotBuilder(state=SnapshotState()), steering=steering)
    acknowledged = []
    workflow = SimpleNamespace(head=lambda _: SimpleNamespace(task_id=h.output.task_id),
        terminal_output=lambda _: h.output, acknowledge_delivery=acknowledged.append)
    queue = VerifierQueue(h.state.root_dir, "run_1", workflow=workflow,
        consume=lambda output: adaptive.accept_verifier_record(output,
            policy=SweSearchUctPolicySpec(policy="swe_search_uct/v1"), max_parallel=2,
            execute=lambda *_: pytest.fail("must not allocate"), steering_host=h.host))
    assert queue.step()
    # A repeated terminal delivery is harmless even without a native receipt.
    assert queue.step()
    assert acknowledged == [h.output.task_id, h.output.task_id]
    assert queue.runner is None
    assert len(h.host.calls) == 1


def test_host_queue_wires_steering_capability_without_selecting_a_branch(setup):
    from goal_plus.session_host import NativeCandidateHost, _unregister_verifier_queue

    h = setup
    received = []
    host = SimpleNamespace(root=h.state.root_dir, _verifier_queues={},
        runtime=SimpleNamespace(
            settlement=SimpleNamespace(verifier_submission=SimpleNamespace(replay=lambda _: None)),
            accept_verifier_record=lambda output, **kw: received.append((output, kw))),
        apply_allocation_decision=lambda *_args, **_kw: None)
    queue = NativeCandidateHost._verifier_queue(host, "run_1", "owner")
    try:
        queue.consume(h.output)
        assert received[0][0] == h.output
        assert isinstance(received[0][1]["steering_host"], _SteeringTransport)
        assert received[0][1]["steering_host"].caller_id == "owner"
    finally:
        _unregister_verifier_queue(host.root, "run_1", queue)


def test_corrupt_attempt_fails_closed(setup, monkeypatch):
    h = setup
    result = accept(h)
    import goal_plus.infrastructure.state.steering as storage
    original = storage.load_json
    def corrupt(path):
        value = original(path)
        value["message_hash"] = "0" * 64
        return value
    monkeypatch.setattr(storage, "load_json", corrupt)
    assert accept(h).status == "error"
    assert len(h.host.calls) == 1
    assert result.attempt_key is not None


def test_delivery_parameters_have_finite_positive_defaults_and_overrides():
    from pydantic import ValidationError
    for bad in [0, -1, float("inf"), float("nan")]:
        with pytest.raises(ValidationError):
            SteeringDeliverySpec(rpc_timeout_seconds=bad, max_source_age_seconds=1)
    defaults = SteeringDeliverySpec()
    assert defaults.rpc_timeout_seconds == 2
    assert defaults.max_source_age_seconds == 60
    assert defaults.mode == "apply"


@pytest.mark.parametrize("harness", ["codex", "pi"])
def test_host_transport_binds_owner_native_generation_and_exact_call(setup, harness):
    h = setup
    calls = []
    control = SimpleNamespace(current_call_id="call_1", session=SimpleNamespace(native_session_id="native_1"),
        wait=lambda call, timeout: SessionCallSnapshot(call, "native_1", "running"),
        message=lambda call, text, timeout: calls.append((call, text, timeout)))
    session = h.state.session.model_copy(update={"agent_harness": harness})
    host = SimpleNamespace(_lock=RLock(), _closing=False,
        _sessions={"agent_1": SimpleNamespace(caller_id="owner", run_id="run_1", scope_id="scope", control=control)},
        admit=lambda *_, **__: session)
    transport = _SteeringTransport(host, "owner")
    target = transport.active_call(session)
    transport.message(target, "guidance", timeout=1, before_send=lambda: None)
    assert calls == [("call_1", "guidance", 1)]
    assert _SteeringTransport(host, "different_owner").active_call(session) is None
    session.execution_generation += 1
    with pytest.raises(PermissionError):
        transport.message(target, "stale", timeout=1, before_send=lambda: None)
    assert len(calls) == 1


def test_transport_source_guard_runs_after_host_admission(setup):
    h = setup
    calls = []
    control = SimpleNamespace(current_call_id="call_1", session=SimpleNamespace(native_session_id="native_1"),
        wait=lambda call, timeout: SessionCallSnapshot(call, "native_1", "running"),
        message=lambda *args, **kwargs: calls.append(args))
    def admit(*args, **kwargs):
        assert h.state.list_attempts("run_1", "c001")
        h.state.candidate.results_ledger_artifact_ref = None
        return h.state.session
    host = SimpleNamespace(_lock=RLock(), _closing=False,
        _sessions={"agent_1": SimpleNamespace(caller_id="owner", run_id="run_1", scope_id="scope", control=control)},
        admit=admit)
    h.host = _SteeringTransport(host, "owner")
    outcome = accept(h)
    assert outcome.reason_code == "source_no_longer_current"
    assert calls == []
    assert h.state.read_attempt("run_1", outcome.attempt_key).observation == "not_sent"
    assert accept(h).reason_code == "already_consumed"
