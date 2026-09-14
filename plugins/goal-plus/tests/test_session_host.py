"""Common product launcher gates and delivery, with controlled native processes."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

from fastmcp import Client
import pytest

from goal_plus.execution.native_session import CodexSession, PiSession
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import close_execution_scope, close_run_candidates, ensure_execution_scope, execution_scope_snapshot
from goal_plus.runtime import FileSearchRuntime
from goal_plus.domain.agents import WorkerBudget
from goal_plus.domain.search import DevicePoolSpec
from goal_plus.infrastructure.search_configuration import DEVICE_POOLS_ENV
from goal_plus.infrastructure.state.resource_locks import VERIFIER_RESOURCE_LOCK_DIR_ENV
from goal_plus.server import create_host_mcp
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal


pytestmark = pytest.mark.host_process


@pytest.fixture(params=["codex", "pi"])
def hosted_candidate(request, tmp_path, monkeypatch):
    settings = request.param if isinstance(request.param, dict) else {"harness": request.param}
    agent_harness = settings["harness"]
    monkeypatch.setenv(VERIFIER_RESOURCE_LOCK_DIR_ENV, str(tmp_path / "resource-locks"))
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\nfrom pathlib import Path\nv = {}\n"
        "exec(Path('initial_program.py').read_text(), v)\n"
        "print(json.dumps({'combined_score': float(v['VALUE'])}))\n"
    )
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project, agent_harness=agent_harness)
    runtime = FileSearchRuntime(root)
    spec = spec_for(project, max_parallel=1)
    spec.strategy.inner_agent = "common"
    spec.strategy.worker_budget = WorkerBudget(max_runtime_seconds=60)
    if settings.get("adaptive"):
        from goal_plus.domain.adaptive.deterministic import PriorityReplacePolicySpec

        spec.workspace.provider = "git_worktree"
        spec.budget.max_candidates = 2
        spec.strategy.orchestration_mode = "adaptive_search"
        spec.strategy.allocation = PriorityReplacePolicySpec(
            features=["latest_record_values/v1"], switch_margin=0,
        )
    spec.device_pools = [DevicePoolSpec(pool_id="test")]
    spec.process_verifiers[0].resource_lock = "test:0"
    monkeypatch.setenv(DEVICE_POOLS_ENV, json.dumps({
        "test": {"visibility_env": "TEST_VISIBLE_DEVICES", "devices": ["assigned"]},
    }))
    monkeypatch.setenv("TEST_VISIBLE_DEVICES", "parent")
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    goals = FileGoalPlusRuntime(root)
    goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    scope = ensure_execution_scope(
        root_dir=root, agent_harness=agent_harness, owner_session_id=goal.active_session.session_id,
         run_id=run_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
        native_agent_config={"model": "main-model", "reasoning_effort": "low"} if agent_harness == "codex"
        else {"model_pattern": "provider/main-model", "thinking_level": "low"},
    )
    actors = []
    inputs = []
    native_actor = CodexSession if agent_harness == "codex" else PiSession

    def actor(config, registration, **kwargs):
        inputs.append(config)
        wire = Path(__file__).parent / "fixtures/native_session_rpc.py"
        scenario = "interrupt" if (tmp_path / "hold-native-turn").exists() else "normal"
        config = replace(config, model=None, reasoning_effort=None, process=replace(
            config.process,
            command=(sys.executable, str(wire), agent_harness, scenario, str(tmp_path / "executed")),
            environment={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
                         "NATIVE_FIXTURE_STATE": str(tmp_path / "native-wire.json"),
                         "NATIVE_FIXTURE_RESUME": "1" if config.native_session_id else "0"},
        ))
        instance = native_actor(config, registration, **kwargs)
        actors.append(instance)
        return instance

    monkeypatch.setattr("goal_plus.session_host." + native_actor.__name__, actor)
    yield root, agent_harness, goal, runtime, task, session, scope, actors, inputs
    for instance in actors:
        instance.close()
    assert close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="test finished")["state"] == "closed"


@pytest.mark.parametrize("hosted_candidate", [
    {"harness": "codex", "adaptive": True}, {"harness": "pi", "adaptive": True},
], indirect=True, ids=["codex", "pi"])
@pytest.mark.parametrize("outcome", ["accepted", "closed", "rejected", "unknown", "lost_ack", "missing_controller"])
def test_allocation_uses_exact_native_cancellation(hosted_candidate, monkeypatch, outcome):
    from goal_plus.bootstrap import build_verifier_record_consumer
    from goal_plus.execution.session_rpc import SessionRequestRejected
    from goal_plus.session_host import NativeCandidateHost
    from tests.test_llm_verifier import invocation
    from goal_plus.application.verifier import VerifierWorkflow
    from goal_plus.infrastructure.state.verifier import FileVerifierState
    from goal_plus.infrastructure.clock import SystemClock

    root, harness, goal, runtime, task, session, scope, actors, _ = hosted_candidate
    (root.parent / "hold-native-turn").touch()
    host = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    try:
        host.run_candidate(task.run_id, task.candidate_id, "first", "work", 30)
        control = host(session.agent_session_id, "wait")
        (task.workspace / "initial_program.py").write_text("VALUE = 1\n")
        runtime.run_verifier(task.run_id, task.candidate_id, agent_session_id=session.agent_session_id,
                             hypothesis="A scored implementation")
        workflow = VerifierWorkflow(FileVerifierState(root), SystemClock())
        claimed = workflow.claim_task("test-verifier", 60, run_id=task.run_id)
        assert claimed is not None
        record = workflow.complete_task(claimed.task_id, {
            "quality_score": 0.8, "allocation_scores": {"progress_reward": 0.8, "allocation_utility": 0.8},
            "rationale": "A deterministic test assessment",
        }, invocation())
        build_verifier_record_consumer(root, verifier=workflow).on_verifier_record_completed(record)
        [decision] = runtime.list_allocation_decisions(task.run_id, "pending")
        before = runtime.list_iterations(task.run_id, task.candidate_id)
        def domain_state():
            return (
                runtime.search_run.state.read_run(task.run_id).model_dump(),
                runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id).model_dump(),
                [item.model_dump() for item in host.queries.sessions(task.run_id)],
                [item.model_dump() for item in runtime.list_allocation_decisions(task.run_id)],
            )

        fenced_state = domain_state()
        for action in (
            lambda: host.run_candidate(task.run_id, task.candidate_id, "second", "continue", 30),
            lambda: runtime.continue_agent_session(session.agent_session_id),
            lambda: runtime.redispatch_candidate(task.run_id, task.candidate_id),
            lambda: runtime.prepare_candidate_recovery(task.run_id, task.candidate_id),
            lambda: runtime.run_verifier(task.run_id, task.candidate_id, agent_session_id=session.agent_session_id,
                                         hypothesis="Forbidden pending retirement"),
        ):
            with pytest.raises(RuntimeError, match="retires|pending allocation"):
                action()
            assert domain_state() == fenced_state
        with pytest.raises(RuntimeError, match="AllocationCancellationRequired"):
            runtime.apply_allocation_decision(task.run_id, decision.decision_id)
        calls = []
        original = control.interrupt

        def interrupt(call_id, *, timeout):
            calls.append(call_id)
            if outcome == "rejected":
                raise SessionRequestRejected("native cancellation rejected")
            if outcome == "unknown":
                raise TimeoutError("native cancellation acknowledgement lost")
            original(call_id, timeout=timeout)
            if outcome == "lost_ack":
                raise TimeoutError("native cancellation acknowledgement lost")

        monkeypatch.setattr(control, "interrupt", interrupt)
        if outcome == "closed":
            original("first", timeout=5)
            assert control.wait("first", timeout=5).state != "running"
            control.close()
        if outcome == "missing_controller":
            detached = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
            with pytest.raises(RuntimeError, match="RecoveryRequired"):
                detached.cancel_allocation(task.run_id, decision.decision_id)
        elif outcome not in {"accepted", "closed"}:
            with pytest.raises((SessionRequestRejected, TimeoutError)):
                host.cancel_allocation(task.run_id, decision.decision_id)
        else:
            host.cancel_allocation(task.run_id, decision.decision_id)
            host.cancel_allocation(task.run_id, decision.decision_id)
            assert calls == ([] if outcome == "closed" else ["first"])
            applied = runtime.apply_allocation_decision(task.run_id, decision.decision_id)
            assert applied["decision"]["status"] == "applied"
            assert runtime.apply_allocation_decision(task.run_id, decision.decision_id) == applied
            [child] = applied["agent_sessions"]
            assert child["agent_harness"] == harness and child["runtime_provider"] == "direct"
            assert child["candidate_id"] != task.candidate_id
            assert control.wait("first", timeout=5).state != "running"
            assert runtime.list_iterations(task.run_id, task.candidate_id) == before
            host(session.agent_session_id, "close").close()
            (root.parent / "hold-native-turn").unlink()
            (Path(child["workspace"]) / "initial_program.py").write_text("VALUE = 2\n")
            host.run_candidate(task.run_id, child["candidate_id"], "child-first", "continue", 10)
            delivered = host(child["agent_session_id"], "wait").wait("child-first", timeout=10)
            assert delivered.state == "completed", delivered.error
            derived = runtime.list_iterations(task.run_id, child["candidate_id"])
            assert derived[-1]["agent_session_id"] == child["agent_session_id"]
            assert derived[-1]["process_passed"]
        receipt = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["allocation_cancellations"][-1]
        assert receipt["agent_session_id"] == session.agent_session_id
        assert receipt["execution_generation"] == session.execution_generation
        assert receipt["call_id"] == "first"
        expected_status = "accepted" if outcome == "closed" else "unknown" if outcome in {"missing_controller", "lost_ack"} else outcome
        assert receipt["status"] == expected_status
        if outcome not in {"accepted", "closed"}:
            assert runtime.list_allocation_decisions(task.run_id)[0].status == "pending"
            with pytest.raises(RuntimeError, match="AllocationCancellationRequired"):
                runtime.apply_allocation_decision(task.run_id, decision.decision_id)
            assert runtime.list_iterations(task.run_id, task.candidate_id) == before
            if outcome == "unknown":
                with pytest.raises(RuntimeError, match="RecoveryRequired"):
                    host.cancel_allocation(task.run_id, decision.decision_id)
                assert calls == ["first"]
            if outcome == "lost_ack":
                assert control.wait("first", timeout=5).state != "running"
                host.cancel_allocation(task.run_id, decision.decision_id)
                assert calls == ["first"]
                assert runtime.apply_allocation_decision(task.run_id, decision.decision_id)["decision"]["status"] == "applied"
            if outcome == "rejected":
                monkeypatch.setattr(control, "interrupt", original)
                host.cancel_allocation(task.run_id, decision.decision_id)
                assert runtime.apply_allocation_decision(task.run_id, decision.decision_id)["decision"]["status"] == "applied"
    finally:
        host.shutdown()


def test_product_mcp_launch_two_calls_and_exact_delivery(hosted_candidate):
    root, agent_harness, goal, runtime, task, session, scope, actors, inputs = hosted_candidate
    assert not {"environment", "cwd", "root", "session_persistence", "metrics_baseline"}.intersection(session.launch)
    mcp = create_host_mcp(root, agent_harness=agent_harness, caller_role="main")

    async def exercise():
        async with Client(mcp) as client:
            async def call(action, **args):
                response = await client.call_tool(
                    "goal_plus_session_" + action, args,
                    meta={"threadId": goal.active_session.session_id},
                )
                return json.loads(response.content[0].text)

            for iteration in (1, 2):
                (task.workspace / "initial_program.py").write_text(f"VALUE = {iteration}\n")
                request = dict(run_id=task.run_id, candidate_id=task.candidate_id, call_id=str(iteration), prompt="bounded task", timeout_seconds=5)
                await call("run", **request)
                result = await call("wait", session_id=session.agent_session_id, call_id=str(iteration), timeout_seconds=10)
                assert result["state"] == "completed", result.get("error") or result
                assert result["result_available"] is True
                detailed = await call("wait", session_id=session.agent_session_id,
                                      call_id=str(iteration), detail=True)
                assert detailed["result"]["text"] == f"answer-{iteration}"
                assert await call("run", **request) == {"session_id": session.agent_session_id, **result}
                records = runtime.list_iterations(task.run_id, task.candidate_id)
                assert len(records) == iteration
                assert records[-1]["agent_session_id"] == session.agent_session_id
                record = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
                assert record.score_report.aggregate_score == iteration
                observed = runtime.candidate_sessions.session(session.agent_session_id)
                assert observed.session_handle.metadata["dispatch_count"] == iteration
                assert "metrics_baseline" not in observed.launch
                if agent_harness == "pi":
                    from goal_plus.host_observability import collect_pi_observability

                    usage = collect_pi_observability(observed)["usage"]
                    assert usage["assistant_messages"] == iteration * 2
                    assert usage["input_tokens"] == (3 if iteration == 1 else 9)
            assert runtime.candidate_sessions.session(session.agent_session_id).counters["resume_dispatches"] == 1
            if agent_harness == "codex":
                from goal_plus.host_scope import _codex_run_is_durably_quiescent

                snapshot = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])
                assert not _codex_run_is_durably_quiescent(root, task.run_id, scope=snapshot)

    asyncio.run(exercise())

    assert inputs[0].model == ("main-model" if agent_harness == "codex" else "provider/main-model")
    assert inputs[0].reasoning_effort == "low"
    assert inputs[0].process.environment["TEST_VISIBLE_DEVICES"] == "assigned"
    assert inputs[0].process.cwd == task.workspace
    assert inputs[0].process.environment["GOAL_PLUS_ROOT"] == str(root)
    if agent_harness == "codex":
        assert inputs[0].process.environment["GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID"] == session.agent_session_id
    assert actors[0].rpc.process.returncode is not None
    assert all(item["status"] == "finished" for item in execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["processes"])
    if agent_harness == "codex":
        from goal_plus.host_scope import _codex_run_is_durably_quiescent

        snapshot = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])
        assert not _codex_run_is_durably_quiescent(root, task.run_id, scope={**snapshot, "processes": []})
        unrelated = [{**item, "label": "native-session:another-candidate"} for item in snapshot["processes"]]
        assert not _codex_run_is_durably_quiescent(root, task.run_id, scope={**snapshot, "processes": unrelated})
    assert close_run_candidates(
        root_dir=root, scope_id=scope["scope_id"], run_id=task.run_id,
    )["active_count"] == 0
    from goal_plus.reporting import build_html_report_data

    report = build_html_report_data(root, task.run_id)
    reported = report["search_tasks"][0]["sessions"]
    assert len(reported) == 2
    assert [item["processed_tokens"] for item in reported] == ([110, 110] if agent_harness == "codex" else [6, 9])


@pytest.mark.parametrize("ending", ["timeout", "stop", "completed_stop"])
def test_run_resumes_same_native_session_after_process_exit(hosted_candidate, ending):
    from goal_plus.session_host import NativeCandidateHost

    root, harness, goal, runtime, task, session, scope, actors, inputs = hosted_candidate
    hold = root.parent / "hold-native-turn"
    if ending != "completed_stop":
        hold.touch()
    host = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    try:
        (task.workspace / "initial_program.py").write_text("VALUE = 3\n")
        runtime.run_verifier(task.run_id, task.candidate_id, agent_session_id=session.agent_session_id,
                             hypothesis="Preserve settled work across native restart")
        before = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
        first = host.run_candidate(task.run_id, task.candidate_id, "first", "work", 0.2 if ending == "timeout" else 10)
        control = host(first["session_id"], "wait")
        if ending == "stop":
            deadline = time.monotonic() + 5
            while not (root.parent / "active").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            control.stop()
        settled = control.wait("first", timeout=10)
        if ending == "completed_stop":
            assert settled.state == "completed"
            control.stop()
        else:
            assert settled.state == "failed"
        assert actors[0].rpc.process.returncode is not None
        assert host.run_candidate(task.run_id, task.candidate_id, "first", "work", 0.2 if ending == "timeout" else 10)["state"] == settled.state
        assert len(actors) == 1
        hold.unlink(missing_ok=True)
        (task.workspace / "initial_program.py").write_text("VALUE = 4\n")
        resumed = host.run_candidate(task.run_id, task.candidate_id, "second", "continue", 5)
        result = control.wait("second", timeout=10)
        assert result.state == "completed", result.error
        assert resumed["session_id"] == first["session_id"]
        assert result.native_session_id == settled.native_session_id
        assert len(actors) == 2
        assert inputs[-1].native_session_id == settled.native_session_id
        if harness == "pi":
            assert "--session" in inputs[-1].process.command
            assert "--session-id" not in inputs[-1].process.command
        assert control.wait("first", timeout=0) == settled
        if ending == "completed_stop":
            assert result.result.invocation_id != settled.result.invocation_id
            assert result.result.usage["scope"] == "since_last_observation"
            assert result.result.usage["input"] == (80 if harness == "codex" else 6)
            if harness == "pi":
                assert result.result.observation.pi_metrics["usage_total"]["input"] == 9
        after = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
        assert after.execution_generation == before.execution_generation
        assert after.iterations[:len(before.iterations)] == before.iterations
        assert len(after.iterations) == len(before.iterations) + 1
        assert len(host.queries.sessions(task.run_id, task.candidate_id)) == 1
        assert runtime.candidate_sessions.session(session.agent_session_id).counters["resume_dispatches"] == 1
    finally:
        host.shutdown()
    assert all(item["status"] == "finished" for item in execution_scope_snapshot(
        root_dir=root, scope_id=scope["scope_id"],
    )["processes"])


def test_same_call_retries_native_resume_after_cleanup_failure(hosted_candidate, monkeypatch):
    from goal_plus.session_host import NativeCandidateHost

    root, harness, goal, runtime, task, session, _, actors, _ = hosted_candidate
    hold = root.parent / "hold-native-turn"
    hold.touch()
    host = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    try:
        host.run_candidate(task.run_id, task.candidate_id, "first", "work", 0.2)
        control = host(session.agent_session_id, "wait")
        assert control.wait("first", timeout=10).state == "failed"
        hold.unlink()
        before = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
        close = actors[0].close
        monkeypatch.setattr(actors[0], "close", lambda: (_ for _ in ()).throw(OSError("cleanup failed")))
        with pytest.raises(OSError, match="cleanup failed"):
            host.run_candidate(task.run_id, task.candidate_id, "retry", "continue", 5)
        assert len(actors) == 1
        assert runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id) == before
        monkeypatch.setattr(actors[0], "close", close)
        host.run_candidate(task.run_id, task.candidate_id, "retry", "continue", 5)
        result = control.wait("retry", timeout=10)
        assert result.state == "completed", result.error
        assert len(actors) == 2
    finally:
        host.shutdown()


def test_run_redispatch_preserves_evidence_and_rejects_old_request(hosted_candidate):
    from goal_plus.session_host import NativeCandidateHost

    root, harness, goal, runtime, task, session, _, actors, _ = hosted_candidate
    host = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    try:
        (task.workspace / "initial_program.py").write_text("VALUE = 1\n")
        first = host.run_candidate(task.run_id, task.candidate_id, "first", "work", 10)
        assert host(first["session_id"], "wait").wait("first", timeout=10).state == "completed"
        before = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
        assert len(before.iterations) == 1
        second = host.run_candidate(task.run_id, task.candidate_id, "fresh", "work", 10, True)
        assert second["session_id"] != first["session_id"]
        assert actors[0].rpc.process.returncode is not None
        assert host(second["session_id"], "wait").wait("fresh", timeout=10).state == "completed"
        assert host.run_candidate(task.run_id, task.candidate_id, "fresh", "work", 10, True)["state"] == "completed"
        after = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
        assert after.execution_generation == before.execution_generation + 1
        assert after.iterations == before.iterations
        assert after.results_ledger == before.results_ledger
        with pytest.raises(RuntimeError, match="StaleAgentSession"):
            host.run_candidate(task.run_id, task.candidate_id, "first", "work", 10)
        with pytest.raises(ValueError, match="SessionRunConflict"):
            host.run_candidate(task.run_id, task.candidate_id, "fresh", "different", 10, True)
        assert len(actors) == 2
    finally:
        host.shutdown()


def test_run_rejects_active_dispatch_and_cleanup_failure(hosted_candidate, monkeypatch):
    from threading import Event
    from goal_plus.session_host import NativeCandidateHost

    root, harness, goal, runtime, task, session, _, actors, _ = hosted_candidate
    host = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    entered, release = Event(), Event()
    review = host.delivery.review

    def blocked_review(*args):
        entered.set()
        assert release.wait(10)
        return review(*args)

    monkeypatch.setattr(host.delivery, "review", blocked_review)
    try:
        host.run_candidate(task.run_id, task.candidate_id, "active", "work", 10)
        assert entered.wait(5)
        before = runtime.candidate_sessions.session(session.agent_session_id).model_dump()
        for fresh in (False, True):
            with pytest.raises(RuntimeError, match="active invocation"):
                host.run_candidate(task.run_id, task.candidate_id, "other", "work", 10, fresh)
        assert runtime.candidate_sessions.session(session.agent_session_id).model_dump() == before
        assert len(actors) == 1
        release.set()
        control = host(session.agent_session_id, "wait")
        assert control.wait("active", timeout=10).state == "completed"
        close = control.session.close
        monkeypatch.setattr(control.session, "close", lambda: (_ for _ in ()).throw(OSError("cleanup failed")))
        with pytest.raises(OSError, match="cleanup failed"):
            host.run_candidate(task.run_id, task.candidate_id, "new", "work", 10, True)
        assert runtime.candidate_sessions.session(session.agent_session_id).execution_generation == 0
        assert len(actors) == 1
        monkeypatch.setattr(control.session, "close", close)
        new = host.run_candidate(task.run_id, task.candidate_id, "new", "work", 10, True)
        assert new["session_id"] != session.agent_session_id
        assert host(new["session_id"], "wait").wait("new", timeout=10).state == "completed"
    finally:
        release.set()
        host.shutdown()


def test_run_prepares_first_session_without_a_model_preparation_step(hosted_candidate):
    from goal_plus.session_host import NativeCandidateHost

    root, harness, _, existing, old_task, _, _, actors, _ = hosted_candidate
    spec = existing.search_run.state.read_frozen_spec(existing.search_run.state.read_run(old_task.run_id).frozen_spec_id).spec
    spec.device_pools = []
    spec.process_verifiers[0].resource_lock = None
    fresh_root = root.parent / "fresh-runtime"
    goal = start_host_verified_goal(fresh_root, Path(spec.source_path), agent_harness=harness)
    runtime = FileSearchRuntime(fresh_root)
    frozen = runtime.freeze_spec(spec, [Path(spec.source_path) / "evaluator.py"], goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    FileGoalPlusRuntime(fresh_root).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, 1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    scope = ensure_execution_scope(root_dir=fresh_root, agent_harness=harness,
        owner_session_id=goal.active_session.session_id, run_id=run_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
        native_agent_config={"model": "main-model", "reasoning_effort": "low"} if harness == "codex"
        else {"model_pattern": "provider/main-model", "thinking_level": "low"})
    host = NativeCandidateHost(fresh_root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    try:
        assert not host.queries.sessions(run_id, task.candidate_id)
        receipt = host.run_candidate(run_id, task.candidate_id, "first", "work", 10)
        assert host(receipt["session_id"], "wait").wait("first", timeout=10).state == "completed"
        assert host.queries.session(receipt["session_id"]).session_handle.external_id == "session"
        assert len(actors) == 1
    finally:
        host.shutdown()
        close_execution_scope(root_dir=fresh_root, scope_id=scope["scope_id"], reason="test finished")


def test_candidate_catalog_omits_disabled_capabilities(hosted_candidate):
    from goal_plus.server import create_mcp
    from tests._fastmcp_helpers import tools_by_name

    root, _, _, _, _, session, _, _, _ = hosted_candidate
    names = asyncio.run(tools_by_name(create_mcp(root, caller_role="candidate", caller_agent_session_id=session.agent_session_id)))
    assert len(names) == 5
    assert not {"goal_plus_search_get_evidence_detail", "goal_plus_search_stage_shared_tool", "goal_plus_search_copy_shared_tool"} & names.keys()


def test_run_startup_failure_retains_request_identity(hosted_candidate, monkeypatch):
    from goal_plus.session_host import NativeCandidateHost

    root, harness, goal, runtime, task, session, _, actors, _ = hosted_candidate
    host = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    native_name = "CodexSession" if harness == "codex" else "PiSession"
    with monkeypatch.context() as failed_start:
        failed_start.setattr("goal_plus.session_host." + native_name,
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("native startup failed")))
        with pytest.raises(OSError, match="native startup failed"):
            host.run_candidate(task.run_id, task.candidate_id, "first", "work", 10)
    try:
        retained = runtime.candidate_sessions.session(session.agent_session_id)
        assert [request.call_id for request in retained.run_requests] == ["first"]
        with pytest.raises(ValueError, match="SessionRunConflict"):
            host.run_candidate(task.run_id, task.candidate_id, "first", "different", 10)
        receipt = host.run_candidate(task.run_id, task.candidate_id, "first", "work", 10)
        assert receipt["session_id"] == session.agent_session_id
        assert host(receipt["session_id"], "wait").wait("first", timeout=10).state == "completed"
        assert len(actors) == 1
        host.shutdown()
        before = runtime.candidate_sessions.session(session.agent_session_id).model_dump()
        with pytest.raises(RuntimeError, match="host is closing"):
            host.run_candidate(task.run_id, task.candidate_id, "after-close", "work", 10)
        assert runtime.candidate_sessions.session(session.agent_session_id).model_dump() == before
    finally:
        host.shutdown()


def test_product_mcp_rejects_missing_foreign_and_fenced_main(hosted_candidate):
    root, agent_harness, goal, runtime, task, session, scope, actors, _ = hosted_candidate
    mcp = create_host_mcp(root, agent_harness=agent_harness, caller_role="main")

    def domain():
        return {str(path): path.read_bytes() for path in (root / "runs").rglob("*.json")}

    async def exercise():
        async with Client(mcp) as client:
            before = domain()
            for meta in (None, {"threadId": "peer"}):
                result = await client.call_tool(
                    "goal_plus_session_run", {"run_id": task.run_id, "candidate_id": task.candidate_id, "call_id": "initial", "prompt": "work", "timeout_seconds": 5},
                    meta=meta, raise_on_error=False,
                )
                assert result.is_error
                assert not actors
                assert domain() == before
            meta = {"threadId": goal.active_session.session_id}
            await client.call_tool("goal_plus_session_run", {"run_id": task.run_id, "candidate_id": task.candidate_id, "call_id": "initial", "prompt": "work", "timeout_seconds": 5}, meta=meta)
            await client.call_tool("goal_plus_session_wait", {"session_id": session.agent_session_id, "call_id": "initial", "timeout_seconds": 10}, meta=meta)
            close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="owner stopped")
            before = domain()
            denied = await client.call_tool("goal_plus_session_run", {
                "run_id": task.run_id, "candidate_id": task.candidate_id, "call_id": "fenced", "prompt": "work", "timeout_seconds": 5,
            }, meta=meta, raise_on_error=False)
            assert denied.is_error
            assert domain() == before
            # Cleanup remains reachable after the execution fence.
            closed = await client.call_tool("goal_plus_session_close", {"session_id": session.agent_session_id}, meta=meta)
            assert json.loads(closed.content[0].text)["closed"]

    asyncio.run(exercise())


def test_rebound_native_handle_fences_old_controller_but_allows_cleanup(hosted_candidate):
    root, agent_harness, goal, runtime, task, session, scope, actors, _ = hosted_candidate
    mcp = create_host_mcp(root, agent_harness=agent_harness, caller_role="main")

    async def exercise():
        async with Client(mcp) as client:
            meta = {"threadId": goal.active_session.session_id}
            await client.call_tool("goal_plus_session_run", {"run_id": task.run_id, "candidate_id": task.candidate_id, "call_id": "initial", "prompt": "work", "timeout_seconds": 5}, meta=meta)
            await client.call_tool("goal_plus_session_wait", {"session_id": session.agent_session_id, "call_id": "initial", "timeout_seconds": 10}, meta=meta)
            runtime.candidate_sessions.bind_agent_handle(session.agent_session_id, {
                "agent_harness": session.agent_harness, "external_id": "replacement-native-session",
                "metadata": {"transport": "app-server" if agent_harness == "codex" else "native-rpc"},
            })
            before = runtime.candidate_sessions.session(session.agent_session_id).model_dump()
            for action, args in (
                ("run", {"run_id": task.run_id, "candidate_id": task.candidate_id, "call_id": "old", "prompt": "work", "timeout_seconds": 5}),
                ("message", {"session_id": session.agent_session_id, "call_id": "old", "prompt": "work", "timeout_seconds": 5}),
            ):
                result = await client.call_tool("goal_plus_session_" + action, args, meta=meta, raise_on_error=False)
                assert result.is_error
                assert "native session handle" in result.content[0].text
                assert runtime.candidate_sessions.session(session.agent_session_id).model_dump() == before
            result = await client.call_tool("goal_plus_session_close", {"session_id": session.agent_session_id}, meta=meta)
            assert json.loads(result.content[0].text)["closed"]
            assert actors[0].rpc.process.returncode is not None

    asyncio.run(exercise())


@pytest.mark.parametrize("scope_stop", [False, True])
def test_cold_controller_recovery_preserves_evidence_and_fences_old_session(hosted_candidate, scope_stop):
    root, agent_harness, goal, runtime, task, session, scope, actors, _ = hosted_candidate
    from goal_plus.host_recovery import approve_recovery

    async def call(client, name, **args):
        response = await client.call_tool(name, args, meta={"threadId": goal.active_session.session_id})
        return json.loads(response.content[0].text)

    async def exercise():
        async with Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main")) as client:
            (task.workspace / "initial_program.py").write_text("VALUE = 1\n")
            await call(client, "goal_plus_session_run", run_id=task.run_id, candidate_id=task.candidate_id,
                       call_id="first", prompt="bounded task", timeout_seconds=5)
            result = await call(client, "goal_plus_session_wait", session_id=session.agent_session_id,
                                call_id="first", timeout_seconds=10)
            assert result["state"] == "completed", result
            before = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
            (task.workspace / "initial_program.py").write_text("VALUE = 99 # partial\n")
            recovery = runtime.prepare_candidate_recovery(task.run_id, task.candidate_id)
            with pytest.raises(RuntimeError, match="RecoverySessionNotReleased"):
                approve_recovery(root, recovery)
            if scope_stop:
                from goal_plus.bootstrap import build_scope_process_registry
                build_scope_process_registry(root).release(scope_id=scope["scope_id"])
                assert actors[0].rpc.process.poll() is None
                assert execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["processes"][0]["status"] == "released"
                approve_recovery(root, recovery)
        assert actors[0].rpc.process.returncode is not None
        async with Client(create_host_mcp(root, agent_harness=agent_harness, caller_role="main")) as client:
            await call(client, "goal_plus_search_recover_candidate", run_id=task.run_id, candidate_id=task.candidate_id,
                       recovery_id=recovery.recovery_id)
            restored = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
            assert restored.iterations == before.iterations
            assert restored.results_ledger == before.results_ledger
            assert (task.workspace / "initial_program.py").read_text() == "VALUE = 1\n"
            fresh = runtime.start_agent_session(task.run_id, task.candidate_id)
            assert fresh.agent_session_id != session.agent_session_id
            assert fresh.execution_generation == session.execution_generation + 1
            assert runtime.candidate_sessions.invocation_timeout(fresh.agent_session_id, 60) <= 60
            assert not (task.workspace / "results.tsv").exists()
            denied = await client.call_tool("goal_plus_session_run", {"run_id": task.run_id, "candidate_id": task.candidate_id, "call_id": "first", "prompt": "bounded task", "timeout_seconds": 5},
                                           meta={"threadId": goal.active_session.session_id}, raise_on_error=False)
            assert denied.is_error
            (task.workspace / "initial_program.py").write_text("VALUE = 2\n")
            await call(client, "goal_plus_session_run", run_id=task.run_id, candidate_id=task.candidate_id,
                       call_id="recovered", prompt="bounded task", timeout_seconds=5)
            result = await call(client, "goal_plus_session_wait", session_id=fresh.agent_session_id,
                                call_id="recovered", timeout_seconds=10)
            assert result["state"] == "completed", result
            after = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
            assert after.iterations[0] == before.iterations[0]
            assert after.iterations[-1].agent_session_id == fresh.agent_session_id
            assert runtime.status(task.run_id).candidates_total == 1

    asyncio.run(exercise())
