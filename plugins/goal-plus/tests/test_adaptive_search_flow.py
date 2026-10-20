"""Direct terminal-record delivery through AdaptiveSearch."""

from types import SimpleNamespace

import pytest

from goal_plus.adaptive.search import AdaptiveSearch


class Snapshots:
    def __init__(self) -> None:
        self.appended = []
        self.state = SimpleNamespace(errors=[])
        self.state.write_policy_error = (
            lambda snapshot, error: self.state.errors.append((snapshot, error))
        )

    def append(self, record, **parameters):
        self.appended.append((record, parameters))
        return SimpleNamespace(run_id="run_1", snapshot_revision="snapshot_1")


class Allocation:
    def __init__(self, decision=None) -> None:
        self.decision = decision
        self.recorded = []

    def record(self, snapshot, plan):
        self.recorded.append((snapshot, plan))
        return self.decision

    def list(self, run_id, status=None):
        return []

    def apply(self, run_id, decision_id, *, cancel, dispatch):
        return None


def parameters():
    return dict(
        policy=SimpleNamespace(policy="swe_search_uct/v1"),
        max_parallel=2,
    )


def test_failed_record_advances_snapshot_without_policy_or_execution(monkeypatch):
    snapshots = Snapshots()
    allocation = Allocation()
    adaptive = AdaptiveSearch(allocation=allocation, snapshots=snapshots)
    record = SimpleNamespace(outcome="failed")
    monkeypatch.setattr(
        "goal_plus.adaptive.search.evaluate_allocation_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed records are not evaluated")
        ),
    )

    adaptive.accept_verifier_record(
        record,
        **parameters(),
        execute=lambda *_args: (_ for _ in ()).throw(
            AssertionError("failed records are not executed")
        ),
    )

    assert snapshots.appended[0][0] is record
    assert allocation.recorded == []


def test_policy_error_is_recorded_and_returns_normally(monkeypatch):
    snapshots = Snapshots()
    allocation = Allocation()
    adaptive = AdaptiveSearch(allocation=allocation, snapshots=snapshots)
    monkeypatch.setattr(
        "goal_plus.adaptive.search.evaluate_allocation_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("deterministic policy failed")
        ),
    )

    adaptive.accept_verifier_record(
        SimpleNamespace(outcome="completed"),
        **parameters(),
        execute=lambda *_args: None,
    )

    assert snapshots.state.errors[0][1] == "RuntimeError: deterministic policy failed"
    assert allocation.recorded == []


def test_record_triggers_one_synchronous_executor_call(monkeypatch):
    decision = SimpleNamespace(
        run_id="run_1", decision_id="allocation_1", status="pending"
    )
    snapshots = Snapshots()
    allocation = Allocation(decision)
    adaptive = AdaptiveSearch(allocation=allocation, snapshots=snapshots)
    plan = SimpleNamespace(plan_hash="plan_1")
    monkeypatch.setattr(
        "goal_plus.adaptive.search.evaluate_allocation_policy",
        lambda *_args, **_kwargs: plan,
    )
    executions = []

    adaptive.accept_verifier_record(
        SimpleNamespace(outcome="completed"),
        **parameters(),
        execute=lambda run_id, decision_id: executions.append(
            (run_id, decision_id)
        ),
    )

    assert allocation.recorded[0][1] is plan
    assert executions == [("run_1", "allocation_1")]


def test_durable_decision_prevents_policy_replay_and_executor_retry(monkeypatch):
    snapshots = Snapshots()
    decision = SimpleNamespace(based_on_snapshot_revision="snapshot_1", status="interrupted")
    allocation = Allocation(decision)
    allocation.list = lambda _: [decision]
    adaptive = AdaptiveSearch(allocation=allocation, snapshots=snapshots)
    monkeypatch.setattr("goal_plus.adaptive.search.evaluate_allocation_policy",
                        lambda *_a, **_k: pytest.fail("durable handoff must not recompute"))
    adaptive.accept_verifier_record(SimpleNamespace(outcome="completed"), **parameters(),
                                    execute=lambda *_: pytest.fail("Executor owns recovery"))


def test_runtime_routes_record_through_real_search_queries_state(tmp_path, monkeypatch):
    from goal_plus.runtime import FileSearchRuntime

    runtime = FileSearchRuntime(tmp_path / ".gp")
    assert not hasattr(runtime.queries, "source")
    allocation = SimpleNamespace(policy="swe_search_uct/v1")
    frozen = SimpleNamespace(spec=SimpleNamespace(
        strategy=SimpleNamespace(
            adaptive_search_enabled=True, allocation=allocation, steering=None
        ),
        budget=SimpleNamespace(max_parallel=2),
    ))
    monkeypatch.setattr(runtime.queries.state, "read_frozen_spec", lambda _: frozen)
    received = []
    runtime._adaptive_search = SimpleNamespace(
        accept_verifier_record=lambda record, **kwargs: received.append((record, kwargs)))
    record = SimpleNamespace(subject=SimpleNamespace(frozen_spec_id="frozen_1"))
    execute = lambda *_: None

    runtime.accept_verifier_record(record, execute=execute)

    assert received == [(record, {
        "policy": allocation,
        "max_parallel": 2,
        "execute": execute,
        "steering_host": None,
        "steering_policy": None,
    })]
