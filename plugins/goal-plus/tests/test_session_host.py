"""Common product launcher gates and delivery, with controlled native processes."""

import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sys
import time

from fastmcp import Client
import pytest

from goal_plus.execution.native_session import CodexSession, PiSession
from goal_plus.execution.model_failures import normalize_model_failure
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import close_execution_scope, close_run_candidates, ensure_execution_scope, execution_scope_snapshot
from goal_plus.runtime import FileSearchRuntime
from goal_plus.domain.agents import WorkerBudget
from goal_plus.domain.search import DevicePoolSpec
from goal_plus.infrastructure.search_configuration import DEVICE_POOLS_ENV
from goal_plus.infrastructure.state.resource_locks import VERIFIER_RESOURCE_LOCK_DIR_ENV
from goal_plus.ports.session import SessionInvocationObservation, SessionInvocationResult
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
        from goal_plus.domain.adaptive.policy import SweSearchUctPolicySpec
        from goal_plus.domain.verifier import VerifierSpec

        spec.workspace.provider = "git_worktree"
        spec.budget.max_candidates = 2
        spec.strategy.orchestration_mode = "adaptive_search"
        spec.strategy.allocation = SweSearchUctPolicySpec(policy="swe_search_uct/v1")
        spec.strategy.verifier = VerifierSpec(scores=["progress_reward", "allocation_utility"])
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


def test_allocation_dispatch_wakes_exact_call_once(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from goal_plus.session_host import NativeCandidateHost

    host = NativeCandidateHost(tmp_path, agent_harness="codex", caller=lambda: "main")
    calls, requests = [], []

    class Control:
        def wait(self, call_id, *, timeout):
            if call_id not in calls:
                raise KeyError(call_id)
            return SimpleNamespace(state="running")

        def wake(self, prompt, *, call_id, timeout):
            assert prompt and timeout == 60
            calls.append(call_id)

    session = SimpleNamespace(agent_session_id="agent_child", run_id="run_1",
                              directive={"expansion_id": "expansion_1"},
                              launch={"budget_control": {"max_runtime_seconds": 60}})
    host._sessions["agent_child"] = SimpleNamespace(control=Control(), caller_id="main")
    monkeypatch.setattr(host, "_open", lambda *args, **kwargs: None)
    host.runtime = SimpleNamespace(candidate_sessions=SimpleNamespace(
        invocation_timeout=lambda _session_id: 60,
        record_run_request=lambda *args, **kwargs: requests.append((args, kwargs)),
    ))
    host._dispatch_allocation(session, None, "main")
    host._dispatch_allocation(session, None, "main")
    assert calls == ["expansion_1"]
    assert len(requests) == 1 and requests[0][1] == {"run_locked": True}


@pytest.mark.parametrize(
    ("runtime_provider", "creates_scope"),
    [("direct", False), ("thinkthread", True)],
)
def test_only_thinkthread_execution_can_create_a_missing_scope(
    tmp_path, monkeypatch, runtime_provider, creates_scope
):
    from types import SimpleNamespace

    from goal_plus.session_host import NativeCandidateHost

    goal = SimpleNamespace(
        goal_plus_id="goal_test",
        control=SimpleNamespace(version=3),
    )
    host = NativeCandidateHost.__new__(NativeCandidateHost)
    host.root = tmp_path
    host.agent_harness = "codex"
    host.authorization = SimpleNamespace(
        require_main_search_session=lambda *_args, **_kwargs: goal
    )
    host.queries = SimpleNamespace(
        runtime_provider=lambda _run_id: runtime_provider
    )
    created = []
    scope = {
        "scope_id": "scope_test",
        "agent_harness": "codex",
        "owner_session_id": "main_test",
        "activation_id": "goal_test:3",
    }
    monkeypatch.setattr(
        "goal_plus.session_host.execution_scope_for_run", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        "goal_plus.session_host.ensure_execution_scope",
        lambda **kwargs: created.append(kwargs) or scope,
    )
    monkeypatch.setattr(
        "goal_plus.session_host.assert_scope_admission", lambda *_args, **_kwargs: None
    )

    if creates_scope:
        host._admit_run("run_test", "main_test", execution=True)
        assert created[0]["run_id"] == "run_test"
    else:
        with pytest.raises(PermissionError, match="requires a host execution scope"):
            host._admit_run("run_test", "main_test", execution=True)
        assert created == []


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
                waited = await call("wait_any", calls=[
                    {"session_id": session.agent_session_id, "call_id": str(iteration)},
                ], timeout_seconds=10)
                result = waited["finished"][0]
                assert result["state"] == "completed", result.get("error") or result
                assert result["result_available"] is True
                detailed = (await call("wait_any", calls=[
                    {"session_id": session.agent_session_id, "call_id": str(iteration)},
                ], detail=True))["finished"][0]
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


@pytest.mark.parametrize("hosted_candidate", [
    {"harness": "codex"}, {"harness": "pi"},
], indirect=True)
def test_provider_failure_receipt_reaches_main_and_monitor_without_secret(hosted_candidate, monkeypatch):
    """A failed child turn is observable by Main with its exact, redacted cause."""
    root, harness, goal, runtime, task, session, _, actors, _ = hosted_candidate
    native_actor = CodexSession if harness == "codex" else PiSession
    failure = normalize_model_failure(
        "HTTP 401: Bearer sk-test-secret-value-should-not-persist",
        phase="response", provider="openai", model="gpt-5.6-luna", source="native_rpc",
    )
    assert failure is not None
    expected_failure = asdict(failure)

    def failed_run(self, prompt, *, timeout, on_started=None):
        if on_started is not None:
            on_started()
        return SessionInvocationResult(
            native_session_id=self.native_session_id,
            invocation_id="provider-failure-1",
            state="failed",
            text="",
            error=failure.message,
            observation=SessionInvocationObservation(
                "2026-09-11T12:00:00Z", "2026-09-11T12:00:01Z", 1,
            ),
            failure=failure,
            agent_harness=harness,
            runtime_provider=session.runtime_provider,
        )

    monkeypatch.setattr(native_actor, "run", failed_run)
    mcp = create_host_mcp(root, agent_harness=harness, caller_role="main")

    async def exercise():
        async with Client(mcp) as client:
            meta = {"threadId": goal.active_session.session_id}
            started = await client.call_tool(
                "goal_plus_session_run", {
                    "run_id": task.run_id, "candidate_id": task.candidate_id,
                    "call_id": "provider-failure", "prompt": "work", "timeout_seconds": 5,
                }, meta=meta)
            started_payload = json.loads(started.content[0].text)
            assert started_payload["session_id"] == session.agent_session_id
            waited = await client.call_tool(
                "goal_plus_session_wait_any", {
                    "calls": [{"session_id": session.agent_session_id, "call_id": "provider-failure"}],
                    "timeout_seconds": 5,
                }, meta=meta)
            receipt = json.loads(waited.content[0].text)["finished"][0]
            assert receipt["state"] == "failed"
            assert receipt["failure"] == expected_failure
            assert "sk-test-secret" not in json.dumps(receipt)

    asyncio.run(exercise())
    observed = runtime.candidate_sessions.session(session.agent_session_id)
    assert observed.session_handle.metadata["model_failure"] == expected_failure
    from goal_plus.tools import SearchTools

    snapshot = SearchTools(runtime).goal_plus_monitor_snapshot(run_id=task.run_id, detail=True)
    assert snapshot["subagents"][0]["model_failure"] == expected_failure
    assert runtime.list_iterations(task.run_id, task.candidate_id) == []


@pytest.mark.parametrize("ending", ["timeout", "stop", "completed_stop", "host_restart"])
def test_run_resumes_same_native_session_after_process_exit(hosted_candidate, ending):
    from goal_plus.session_host import NativeCandidateHost

    root, harness, goal, runtime, task, session, scope, actors, inputs = hosted_candidate
    hold = root.parent / "hold-native-turn"
    if ending not in {"completed_stop", "host_restart"}:
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
        if ending in {"completed_stop", "host_restart"}:
            assert settled.state == "completed"
            control.stop()
        else:
            assert settled.state == "failed"
        assert actors[0].rpc.process.returncode is not None
        assert host.run_candidate(task.run_id, task.candidate_id, "first", "work", 0.2 if ending == "timeout" else 10)["state"] == settled.state
        assert len(actors) == 1
        hold.unlink(missing_ok=True)
        (task.workspace / "initial_program.py").write_text("VALUE = 4\n")
        if ending == "host_restart":
            (task.workspace / ".tmp").mkdir(exist_ok=True)
            note = task.workspace / ".tmp/local-result.txt"
            note.write_text("local result not submitted to verifier\n")
            host.shutdown()
            host = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
            with pytest.raises(RuntimeError, match="SessionCallHistoryUnavailable.*new call_id"):
                host(session.agent_session_id, "wait")
            assert (task.workspace / "initial_program.py").read_text() == "VALUE = 4\n"
            assert note.read_text() == "local result not submitted to verifier\n"
            assert runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id).iterations == before.iterations
            agenda = FileGoalPlusRuntime(root).reconciliation(goal.goal_plus_id)
            assert agenda["runs"][0]["candidates"][0]["next_step"] == "continue_candidate"
            with pytest.raises(RuntimeError, match="SessionCallHistoryUnavailable"):
                host.run_candidate(task.run_id, task.candidate_id, "first", "work", 10)
        resumed = host.run_candidate(task.run_id, task.candidate_id, "second", "continue", 5)
        control = host(resumed["session_id"], "wait")
        result = control.wait("second", timeout=10)
        assert result.state == "completed", result.error
        assert resumed["session_id"] == first["session_id"]
        assert result.native_session_id == settled.native_session_id
        assert len(actors) == 2
        assert inputs[-1].native_session_id == settled.native_session_id
        if harness == "pi":
            assert "--session" in inputs[-1].process.command
            assert "--session-id" not in inputs[-1].process.command
        if ending != "host_restart":
            assert control.wait("first", timeout=0) == settled
        if ending in {"completed_stop", "host_restart"}:
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


@pytest.mark.parametrize("host_restart", [False, True])
def test_run_redispatch_preserves_evidence_and_rejects_old_request(hosted_candidate, monkeypatch, host_restart):
    from goal_plus.session_host import NativeCandidateHost

    root, harness, goal, runtime, task, session, _, actors, _ = hosted_candidate
    host = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    try:
        (task.workspace / "initial_program.py").write_text("VALUE = 1\n")
        first = host.run_candidate(task.run_id, task.candidate_id, "first", "work", 10)
        assert host(first["session_id"], "wait").wait("first", timeout=10).state == "completed"
        before = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
        assert len(before.iterations) == 1
        if host_restart:
            host.shutdown()
            host = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
            launch = host._launch_native

            def unavailable(*args, **kwargs):
                if kwargs.get("resume"):
                    raise FileNotFoundError("native session unavailable")
                return launch(*args, **kwargs)

            monkeypatch.setattr(host, "_launch_native", unavailable)
            with pytest.raises(FileNotFoundError, match="native session unavailable"):
                host.open(session.agent_session_id)
            assert runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id) == before
            (task.workspace / ".tmp").mkdir(exist_ok=True)
            (task.workspace / ".tmp/local-result.txt").write_text("unverified local result\n")
        second = host.run_candidate(task.run_id, task.candidate_id, "fresh", "work", 10, True)
        assert second["session_id"] != first["session_id"]
        assert actors[0].rpc.process.returncode is not None
        assert host(second["session_id"], "wait").wait("fresh", timeout=10).state == "completed"
        assert host.run_candidate(task.run_id, task.candidate_id, "fresh", "work", 10, True)["state"] == "completed"
        after = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
        assert after.execution_generation == before.execution_generation + 1
        assert after.iterations == before.iterations
        assert after.results_ledger == before.results_ledger
        if host_restart:
            assert (task.workspace / ".tmp/local-result.txt").read_text() == "unverified local result\n"
        with pytest.raises(RuntimeError, match="StaleAgentSession"):
            host.run_candidate(task.run_id, task.candidate_id, "first", "work", 10)
        with pytest.raises(ValueError, match="SessionRunConflict"):
            host.run_candidate(task.run_id, task.candidate_id, "fresh", "different", 10, True)
        assert len(actors) == 2
    finally:
        host.shutdown()


def test_rebuilt_host_requires_cleanup_and_settled_verifier(hosted_candidate):
    from goal_plus.session_host import NativeCandidateHost

    root, harness, goal, runtime, task, session, scope, actors, _ = hosted_candidate
    first = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    rebuilt = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    try:
        first.open(session.agent_session_id)
        before = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
        from goal_plus.bootstrap import build_scope_process_registry

        for released in (False, True):
            if released:
                build_scope_process_registry(root).release(scope_id=scope["scope_id"])
            assert actors[0].rpc.process.poll() is None
            for new_session in (False, True):
                with pytest.raises(RuntimeError, match="SessionExecutionActive"):
                    rebuilt.run_candidate(task.run_id, task.candidate_id, "resume", "continue", 5, new_session)
                assert runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id) == before
        assert len(actors) == 1
        first.shutdown()
        (task.workspace / "initial_program.py").write_text("VALUE = 4\n")
        def while_pending(attempt):
            pending = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
            assert pending.verification.attempt_id == attempt.attempt_id
            assert runtime.verifier_result(task.run_id, task.candidate_id, attempt.attempt_id,
                                           session.agent_session_id)["state"] == "pending"
            with pytest.raises(RuntimeError, match="VerifierInProgress|VerifierRecoveryRequired"):
                rebuilt.open(session.agent_session_id)
            assert runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id) == pending

        runtime.run_verifier(task.run_id, task.candidate_id, agent_session_id=session.agent_session_id,
                             hypothesis="pending attempt", on_submitted=while_pending)
        assert len(actors) == 1
        assert rebuilt.open(session.agent_session_id).session_id == session.agent_session_id
    finally:
        first.shutdown()
        rebuilt.shutdown()


def test_rebuilt_host_resumes_released_session_after_process_exit(hosted_candidate):
    from goal_plus.bootstrap import build_scope_process_registry
    from goal_plus.session_host import NativeCandidateHost

    root, harness, goal, runtime, task, session, scope, actors, inputs = hosted_candidate
    first = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    rebuilt = NativeCandidateHost(root, agent_harness=harness, caller=lambda: goal.active_session.session_id)
    try:
        opened = first.open(session.agent_session_id)
        before = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
        build_scope_process_registry(root).release(scope_id=scope["scope_id"])
        actors[0].rpc.process.kill()
        actors[0].rpc.process.wait(timeout=5)
        (task.workspace / "initial_program.py").write_text("VALUE = 4\n")

        receipt = rebuilt.run_candidate(task.run_id, task.candidate_id, "resume", "continue", 5)
        result = rebuilt(receipt["session_id"], "wait").wait("resume", timeout=10)

        assert result.state == "completed", result.error
        assert receipt["session_id"] == session.agent_session_id
        assert inputs[-1].native_session_id == opened.native_session_id
        after = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
        assert after.execution_generation == before.execution_generation
        assert after.iterations[:len(before.iterations)] == before.iterations
        resources = [item for item in execution_scope_snapshot(
            root_dir=root, scope_id=scope["scope_id"],
        )["processes"] if item["label"] == f"native-session:{session.agent_session_id}"]
        assert len(resources) == 2 and resources[0]["status"] == "finished"
    finally:
        first.shutdown()
        rebuilt.shutdown()


@pytest.mark.parametrize("hosted_candidate", ["codex"], indirect=True)
def test_verifier_process_loss_requires_inspection_and_recovery(hosted_candidate):
    import subprocess

    root, _, goal, runtime, task, session, _, _, _ = hosted_candidate
    (task.workspace / "initial_program.py").write_text("VALUE = 4\n")
    before = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
    crashed = subprocess.run(
        [sys.executable, "-c", """
import os, sys
from goal_plus.runtime import FileSearchRuntime
FileSearchRuntime(sys.argv[1]).run_verifier(
    sys.argv[2], sys.argv[3], agent_session_id=sys.argv[4], hypothesis="interrupted verification",
    on_submitted=lambda attempt: os._exit(23),
)
""", str(root), task.run_id, task.candidate_id, session.agent_session_id],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        capture_output=True, text=True, timeout=30,
    )
    assert crashed.returncode == 23, crashed.stderr
    pending = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
    assert pending.verification.state == "running"
    assert pending.iterations == before.iterations
    agenda = FileGoalPlusRuntime(root).reconciliation(goal.goal_plus_id)
    assert agenda["runs"][0]["candidates"][0]["next_step"] == "inspect_verifier_execution"
    result = runtime.verifier_result(task.run_id, task.candidate_id, pending.verification.attempt_id,
                                     session.agent_session_id)
    assert result["state"] == "needs_recovery"
    assert result["candidate_action"] == "retry_verifier"
    assert runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id) == pending


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
    assert "goal_plus_search_run_verifier" in names
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
            await client.call_tool("goal_plus_session_wait_any", {"calls": [{"session_id": session.agent_session_id, "call_id": "initial"}], "timeout_seconds": 10}, meta=meta)
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
            await client.call_tool("goal_plus_session_wait_any", {"calls": [{"session_id": session.agent_session_id, "call_id": "initial"}], "timeout_seconds": 10}, meta=meta)
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
            result = await call(client, "goal_plus_session_wait_any",
                                calls=[{"session_id": session.agent_session_id, "call_id": "first"}],
                                timeout_seconds=10)
            assert result["finished"][0]["state"] == "completed", result
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
            result = await call(client, "goal_plus_session_wait_any",
                                calls=[{"session_id": fresh.agent_session_id, "call_id": "recovered"}],
                                timeout_seconds=10)
            assert result["finished"][0]["state"] == "completed", result
            after = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
            assert after.iterations[0] == before.iterations[0]
            assert after.iterations[-1].agent_session_id == fresh.agent_session_id
            assert runtime.status(task.run_id).candidates_total == 1

    asyncio.run(exercise())

@pytest.mark.parametrize("failed_owner", ["annotation", "executor"])
def test_internal_owners_wake_independently(tmp_path, monkeypatch, failed_owner):
    from types import SimpleNamespace
    from goal_plus.session_host import NativeCandidateHost
    host = NativeCandidateHost(tmp_path, agent_harness="codex", caller=lambda: "main")
    events = []
    monkeypatch.setattr(host, "_admit_run", lambda *_a, **_k: None)
    monkeypatch.setattr(host, "_verifier_queue", lambda *_: SimpleNamespace(start=lambda: events.append("verifier")))
    monkeypatch.setattr("goal_plus.bootstrap.build_internal_agent_work",
                        lambda _: SimpleNamespace(inspect=lambda _: SimpleNamespace(accepts_work=True)))
    monkeypatch.setattr("goal_plus.host_scope.run_accepts_internal_agent_launch", lambda **_: True)
    def wake(owner):
        events.append(owner)
        if owner == failed_owner:
            raise OSError("independent owner unavailable")
    monkeypatch.setattr("goal_plus.internal_agents.kick_internal_agents", lambda *_a, **_k: wake("annotation"))
    host.runtime = SimpleNamespace(recover_allocation=lambda *_a, **_k: wake("executor"))
    host.wake_internal_agents("run_test", caller_id="main")
    assert set(events) == {"verifier", "annotation", "executor"}
