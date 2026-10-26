"""Direct native checkpoint admission through the real adapter boundary."""

import json
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import ensure_execution_scope
from goal_plus.infrastructure.direct_fork import DirectDispatchAdapter
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal


@pytest.fixture
def checkpoint_clock(monkeypatch):
    epoch = [1792000000]
    stamp = lambda: datetime.fromtimestamp(epoch[0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr("goal_plus.infrastructure.clock.utc_timestamp", stamp)
    monkeypatch.setattr("goal_plus.infrastructure.state.goal_mutations.utc_timestamp", stamp)
    return epoch


@pytest.mark.parametrize("prepared,selector,boundary,accepted", [
    (False, "latest_completed", "new", False),
    (False, "prepared-turn", "new", False),
    (True, "latest_completed", "new", True),
    (True, "prepared-turn", "new", True),
    (True, "older-turn", "new", False),
    (True, "latest_completed", "old", False),
    (True, "prepared-turn", "same_second", False),
    (True, "latest_completed", "missing_time", False),
    (True, "latest_completed", "blocked_before_batch", False),
    (True, "latest_completed", "blocked_same_second", False),
    (True, "latest_completed", "missing_batch_time", False),
])
def test_direct_checkpoint_selectors_share_preparation_and_completed_boundary(
    tmp_path, monkeypatch, checkpoint_clock, prepared, selector, boundary, accepted,
):
    # The domain fixtures do not mock admission. Only the provider's native
    # history is controlled, so explicit selectors cannot skip the real gate.
    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project)
    runtime = FileSearchRuntime(root)
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"], goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    goals = FileGoalPlusRuntime(root)
    goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    runtime.start_batch(run_id, plan.plan_id)
    if boundary == "missing_batch_time":
        plan = runtime.search_run.state.read_plan(run_id, plan.plan_id)
        plan.started_at = None
        runtime.search_run.state.write_plan(plan)
    checkpoint_clock[0] += {"blocked_before_batch": -1, "blocked_same_second": 0}.get(boundary, 1)
    if prepared:
        goals.set_status(goal.goal_plus_id, "blocked", reason="Finish native preparation turn")
    ensure_execution_scope(
        root_dir=root, agent_harness="codex", owner_session_id=goal.active_session.session_id,
        run_id=run_id, activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
        native_agent_config={"model": "test-model", "reasoning_effort": "low"},
    )
    opened, closed = [], []
    event = next((e for e in reversed(goals.state.read_events(goal.goal_plus_id))
                  if e["event_type"] == "status_changed"), None)
    prepared_epoch = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).timestamp() if event else 0
    completed_at = None if boundary == "missing_time" else int(prepared_epoch) + {"old": -60, "same_second": 0}.get(boundary, 1)

    class NativeHistory:
        def __init__(self, *args, **kwargs):
            opened.append(self)

        def _request(self, method, _params, *, timeout):
            assert method == "thread/read"
            return {"thread": {"turns": [
                {"id": "older-turn", "status": "completed"},
                {"id": "prepared-turn", "status": "completed", "completedAt": completed_at},
                {"id": "current-turn", "status": "inProgress"},
            ]}}

        def close(self):
            closed.append(self)

    monkeypatch.setattr("goal_plus.infrastructure.direct_fork.CodexSession", NativeHistory)
    adapter = DirectDispatchAdapter(root, runtime, "codex", checkpoint_timeout=5)
    before = runtime.search_run.state.read_run(run_id)
    if accepted:
        checkpoint = adapter.prepare_checkpoint(run_id, goal.active_session.session_id, selector)
        assert checkpoint.checkpoint_id == "prepared-turn"
        assert checkpoint.checkpoint_kind == "completed_turn"
    else:
        error, reason = (
            (RuntimeError, "ForkPreparationRequired") if boundary in {"blocked_before_batch", "blocked_same_second", "missing_batch_time"} else
            (RuntimeError, "ForkCheckpointUnavailable") if boundary != "new" else
            (ValueError, "prepared completed checkpoint") if prepared else
            (RuntimeError, "ForkPreparationRequired")
        )
        with pytest.raises(error, match=reason):
            adapter.prepare_checkpoint(run_id, goal.active_session.session_id, selector)
    assert runtime.search_run.state.read_run(run_id) == before
    assert len(opened) == int(prepared and boundary not in {"blocked_before_batch", "blocked_same_second", "missing_batch_time"})
    assert closed == opened


@pytest.mark.parametrize("marker", [True, False])
def test_direct_pi_requires_current_run_preparation_marker(tmp_path, monkeypatch, checkpoint_clock, marker):
    from types import SimpleNamespace

    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project, agent_harness="pi")
    runtime = FileSearchRuntime(root)
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"],
                                agent_harness="pi", goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    goals = FileGoalPlusRuntime(root)
    goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    checkpoint_clock[0] += 1
    goals.set_status(goal.goal_plus_id, "blocked", reason="Preparation complete")
    entries = [{"type": "message", "id": "prepared", "parentId": None,
                "message": {"role": "assistant", "stopReason": "stop", "content": []}}]
    if marker:
        entries.append({"type": "custom", "id": "marker", "parentId": "prepared",
                        "customType": "goal-plus-fork-checkpoint", "data": {"run_id": run_id}})
    native = SimpleNamespace(
        _command=lambda *_args, **_kwargs: {"data": {"entries": entries, "leafId": entries[-1]["id"]}},
        close=lambda: None,
    )
    monkeypatch.setattr(DirectDispatchAdapter, "_open_parent", lambda *_args, **_kwargs: native)
    adapter = DirectDispatchAdapter(root, runtime, "pi", checkpoint_timeout=5)
    before = runtime.search_run.state.read_run(run_id)
    if marker:
        assert adapter.prepare_checkpoint(run_id, goal.active_session.session_id).checkpoint_id == "prepared"
    else:
        with pytest.raises(RuntimeError, match="checkpoint marker"):
            adapter.prepare_checkpoint(run_id, goal.active_session.session_id)
    assert runtime.search_run.state.read_run(run_id) == before


def test_direct_pi_recovery_rebuilds_receipt_from_deterministic_session_file(tmp_path):
    """A Pi fork file survives a host crash before the domain receipt callback."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_id = "agent-1"
    session_file = tmp_path / "host-sessions" / "pi" / f"2026-01-01_{session_id}.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text(
        json.dumps({"type": "session", "id": session_id, "cwd": str(workspace)}) + "\n",
        encoding="utf-8",
    )
    branch = SimpleNamespace(
        agent_session_id=session_id,
        binding=SimpleNamespace(workspace=workspace, launch_receipt={}),
    )
    fork = SimpleNamespace(
        main_session_id="main-session",
        runtime_provider="direct",
        parent_native_session_id="main-session",
        checkpoint_id="turn-7",
        checkpoint_kind="entry",
        targets=[branch],
    )
    session = SimpleNamespace(
        candidate_origin=object(),
        session_handle=SimpleNamespace(external_id=session_id),
        workspace=workspace,
    )
    state = SimpleNamespace(
        read_run=lambda _run_id: SimpleNamespace(dispatch_group=fork),
        read_agent_session=lambda _session_id, _run_id: session,
    )
    runtime = SimpleNamespace(
        search_run=SimpleNamespace(state=state),
        candidate_sessions=SimpleNamespace(state=state),
    )

    adapter = DirectDispatchAdapter(tmp_path, runtime, "pi", checkpoint_timeout=5)
    receipts = adapter.recover_receipts("run-1", "main-session")

    assert len(receipts) == 1
    assert receipts[0].provider_session_id == session_id
    assert receipts[0].native_session_file == str(session_file.resolve())
    assert receipts[0].checkpoint_id == "turn-7"

    # A cleanup receipt is persisted as the immutable creation receipt while
    # the branch state records its separate close fact. Recovery must preserve
    # that distinction so a restart cannot resurrect a cleaned branch.
    branch.binding.launch_receipt = asdict(receipts[0])
    branch.launch_state = "cleaned"
    branch.closed_at = "2026-01-01T00:00:00Z"
    cleaned = adapter.recover_receipts("run-1", "main-session")
    assert cleaned[0].launch_state == "cleaned"
    assert cleaned[0].closed_at == branch.closed_at


def test_direct_codex_recovery_fails_closed_without_persisted_receipt(tmp_path):
    branch = SimpleNamespace(
        agent_session_id="agent-1",
        binding=SimpleNamespace(workspace=tmp_path / "workspace", launch_receipt={}),
    )
    fork = SimpleNamespace(
        main_session_id="main-session",
        runtime_provider="direct",
        parent_native_session_id="main-session",
        checkpoint_id="turn-7",
        checkpoint_kind="completed_turn",
        targets=[branch],
    )
    state = SimpleNamespace(read_run=lambda _run_id: SimpleNamespace(dispatch_group=fork))
    runtime = SimpleNamespace(search_run=SimpleNamespace(state=state))

    adapter = DirectDispatchAdapter(tmp_path, runtime, "codex", checkpoint_timeout=5)
    with pytest.raises(RuntimeError, match="ForkRecoveryIncomplete"):
        adapter.recover_receipts("run-1", "main-session")
