"""Real native Main resume -> same Search lane -> fresh worker generation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

from goal_plus.models import SearchSpec
from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.host_scope import close_execution_scope
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, spec_for
from tests.st.hosts import ST_ACTIVE_ENV, st_model_for_host


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.st


def _wait_for(process, condition, log_path, seconds=600):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        assert process.poll() is None, log_path.read_text(errors="replace")[-6000:]
        time.sleep(0.2)
    pytest.fail(f"Search recovery ST timed out; see {log_path}")


def _stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            managed = ManagedProcess(process)
            managed.close()


@pytest.mark.parametrize("agent_harness", [
    pytest.param("codex", marks=pytest.mark.st_codex),
    pytest.param("pi", marks=pytest.mark.st_pi_rpc),
])
def test_native_main_resume_recovers_active_search(tmp_path, st_log_dir, agent_harness, isolated_pi_config, monkeypatch, request):
    workspace = tmp_path / "recovery"
    workspace.mkdir()
    if agent_harness == "codex":
        home = request.getfixturevalue("installed_codex_config")
        skills = [path for path in (home / "plugins/cache").rglob("SKILL.md")
                  if path.parent.name in {"goal-plus", "search", "goal-plus-with-final-check"}]
        assert not skills
    else:
        monkeypatch.setenv("GOAL_PLUS_PI_DEV_ROOT", str(ROOT))
        monkeypatch.setenv("GOAL_PLUS_PYTHON", str(ROOT / ".goal-plus-dev/venv/bin/python"))
    project = make_project(workspace)
    runtime = FileSearchRuntime(workspace / ".gp")
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["objective"] = (
        "Exercise Search crash recovery. First call goal_plus_search_get_agent_context. "
        "If resume.recovery is null: run the process verifier once, then change "
        "initial_program.py to 'VALUE = 1 # RECOVERY_PARTIAL\\n' WITHOUT committing "
        "or verifying that change. Keep waiting with short sleep calls until the "
        "controller interrupts you; do not undo the partial edit. "
        "If resume.recovery is present: confirm initial_program.py is restored to "
        "VALUE = 0 without RECOVERY_PARTIAL, then run the process verifier once "
        "with hypothesis 'recovered settled baseline' and finish. Never create another candidate."
    )
    data["strategy"].update({"inner_agent": "common", "worker_budget": {"max_runtime_seconds": 240}})
    data["workspace"]["promotion_mode"] = "artifact_only"
    spec = SearchSpec.model_validate(data)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="pi" if agent_harness in {"pi", "thinkthread", "pi"} else "codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    fixture = workspace / "search-fixture.json"
    fixture.write_text(json.dumps({
        "run_id": run_id, "frozen_spec_id": frozen.frozen_spec_id,
        "candidate_id": task.candidate_id, "spec": spec.model_dump(mode="json"),
    }))
    prompt = (
        "Exercise the native Main/Search recovery workflow within 900 seconds. "
        f"The test controller already created exactly one standalone Search run in {fixture}. "
        "Read that file. Record high-confidence optimization triage; save a high-confidence "
        "spec draft with the exact supplied spec and no open questions, then link the existing "
        "run using goal_plus_link_search_run. Do NOT freeze/create/plan/materialize another run "
        "or candidate. Start its one worker and let it follow the frozen objective. "
        "Keep Main active while waiting; the controller will interrupt "
        "and resume this native session after the worker writes its partial attempt. "
        "After recovery has produced fresh verified Evidence from a fresh worker generation, "
        "finish selection, artifact-only promotion, record the Search result and generate its report, "
        "then mark Goal Plus complete with concrete evidence. Before recovery, keep waiting. "
        "Do not modify candidate files yourself, run pytest or nested host CLIs. "
        "Never modify the plugin implementation, host configuration, or anything outside this "
        "temporary workspace. If a runtime/tool fails, report the failure and wait for the controller; "
        "do not patch code, write runtime state, or bypass missing tools with Python/shell scripts."
    )
    env = {**os.environ, ST_ACTIVE_ENV: "native_search_recovery", "GOAL_PLUS_ROOT": str(runtime.root_dir)}
    if agent_harness == "codex":
        initial_command = ["codex", "exec", "--skip-git-repo-check", "--json",
                           "--sandbox", "workspace-write", "-c", 'approval_policy="never"',
                           "-c", 'model_reasoning_effort="low"',
                           "-m", st_model_for_host(agent_harness), "$goal-plus " + prompt]
    else:
        pi_base = ["pi", "--approve", "--session-dir", str(workspace / "sessions"),
                   "--no-skills", "--no-context-files", "--no-extensions", "--thinking", "low",
                   "-e", str(ROOT / "assets/pi/extensions/goal-plus.ts")]
        if os.environ.get("ST_PI_MODEL"):
            pi_base += ["--model", os.environ["ST_PI_MODEL"]]
        initial_command = [*pi_base, "-p", "--mode", "json", "/goal-plus " + prompt]
    initial_log = st_log_dir / f"search-recovery-{agent_harness}-initial.log"
    print(f"Native Main recovery evidence: {st_log_dir}", flush=True)

    def cleanup():
        return [close_execution_scope(root_dir=runtime.root_dir, scope_id=path.parent.name,
                    reason="recovery ST controller stopped Main")
                for path in (runtime.root_dir / "host-scopes").glob("scope_*/scope.json")]

    with initial_log.open("w") as log:
        process = subprocess.Popen(initial_command, cwd=workspace, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            def partial_ready():
                candidate = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
                return (
                    len(candidate.iterations) == 1
                    and "RECOVERY_PARTIAL" in (task.workspace / "initial_program.py").read_text()
                    and any(session.session_handle.metadata.get("transport") in {"app-server", "native-rpc"}
                            for session in runtime.candidate_sessions.state.read_agent_sessions(run_id))
                )
            _wait_for(process, partial_ready, initial_log)
        finally:
            _stop(process)
            initial_cleanup = cleanup()
            (st_log_dir / "initial-cleanup.json").write_text(json.dumps(initial_cleanup, indent=2))
            assert all(item["state"] == "closed" for item in initial_cleanup)
    goal_path, = (runtime.root_dir / "goal-plus").glob("gp_*/goal.json")
    before = json.loads(goal_path.read_text())
    session_id = before["active_session"]["session_id"]
    before_candidate = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    resume_prompt = "$goal-plus resume" if agent_harness == "codex" else "/goal-plus resume"
    if agent_harness == "codex":
        command = ["codex", "exec", "resume", "--skip-git-repo-check",
                   "-c", 'approval_policy="never"', "-c", 'sandbox_mode="workspace-write"',
                   "-c", 'model_reasoning_effort="low"',
                   "--json", "-m", st_model_for_host(agent_harness), session_id, resume_prompt]
    else:
        command = [*pi_base, "--session", session_id, "-p", "--mode", "json", resume_prompt]
    resumed_log = st_log_dir / f"search-recovery-{agent_harness}-resumed.log"
    with resumed_log.open("w") as log:
        process = subprocess.Popen(command, cwd=workspace, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            def recovered():
                candidate = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
                goal = json.loads(goal_path.read_text())
                reports_ready = bool(goal["search_tasks"]) and all(
                    Path(item.get(key) or "missing-report").is_file()
                    for item in goal["search_tasks"] for key in ("report_path", "html_report_path")
                )
                return candidate if (
                    candidate.recovery and candidate.recovery.state == "restored"
                    and len(candidate.iterations) >= 2
                    and candidate.iterations[-1].agent_session_id != before_candidate.iterations[0].agent_session_id
                    and goal["status"] == "complete" and reports_ready
                ) else None
            after_candidate = _wait_for(process, recovered, resumed_log)
        finally:
            _stop(process)
            final_cleanup = cleanup()
            (st_log_dir / "final-cleanup.json").write_text(json.dumps(final_cleanup, indent=2))
            assert all(item["state"] == "closed" for item in final_cleanup)
    after = json.loads(goal_path.read_text())
    assert after["goal_plus_id"] == before["goal_plus_id"]
    assert after["goal_revision"] == before["goal_revision"]
    assert after["outer_deadline_at"] == before["outer_deadline_at"]
    assert runtime.status(run_id).state == "promoted"
    assert after["host_command_invocations"][:-1] == before["host_command_invocations"]
    assert after["host_command_invocations"][-1]["action"] == "resume"
    assert after["active_session"]["session_id"] == session_id
    assert runtime.status(run_id).candidates_total == 1
    assert len(runtime.candidate_sessions.state.read_agent_sessions(run_id)) == 2  # One old worker, one replacement.
    assert after_candidate.iterations[0] == before_candidate.iterations[0]
    assert after_candidate.execution_generation == 1
    assert after_candidate.iterations[-1].agent_session_id != before_candidate.iterations[0].agent_session_id
    assert "RECOVERY_PARTIAL" not in (task.workspace / "initial_program.py").read_text()
    (st_log_dir / "goal.json").write_text(json.dumps(after, indent=2))
    (st_log_dir / "candidate.json").write_text(after_candidate.model_dump_json(indent=2))
