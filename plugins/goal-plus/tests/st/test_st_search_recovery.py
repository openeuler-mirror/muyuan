"""Real native Main resume -> same Search lane -> fresh worker generation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

import pytest

from goal_plus.models import SearchSpec
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, spec_for
from tests.st.helpers.codex_runner import CodexRunner
from tests.st.hosts import ST_ACTIVE_ENV, link_host_assets


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
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


@pytest.fixture(autouse=True)
def _cleanup_owned_pools(st_project_root):
    yield
    from goal_plus.pi_pool import close_pi_search_pool

    # The ST controller owns only its uniquely named temporary runtimes.
    # Clean up live RPC jobs even when an assertion fails before native resume.
    for path in st_project_root.glob("search-recovery-*/.gp/host-pools/pi/*/pool.json"):
        root = path.parents[3]
        close_pi_search_pool(root_dir=root, pool_id=path.parent.name,
                             mode="interrupt", timeout_seconds=10, _allow_early_close=True)


@pytest.mark.parametrize("host", [
    pytest.param("codex", marks=pytest.mark.st_codex),
    pytest.param("pi-rpc", marks=pytest.mark.st_pi_rpc),
])
def test_native_main_resume_recovers_active_search(st_project_root, st_log_dir, host):
    workspace = Path(tempfile.mkdtemp(prefix=f"search-recovery-{host}-", dir=st_project_root))
    if host == "codex":
        link_host_assets(workspace, ROOT)
    project = make_project(workspace)
    runtime = FileSearchRuntime(workspace / ".gp")
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["objective"] = (
        "Exercise Search crash recovery. First call search_get_agent_context. "
        "If resume.recovery is null: run the process verifier once, then change "
        "initial_program.py to 'VALUE = 1 # RECOVERY_PARTIAL\\n' WITHOUT committing "
        "or verifying that change. Keep waiting with short sleep calls until the "
        "controller interrupts you; do not undo the partial edit. "
        "If resume.recovery is present: confirm initial_program.py is restored to "
        "VALUE = 0 without RECOVERY_PARTIAL, then run the process verifier once "
        "with hypothesis 'recovered settled baseline' and finish. Never create another candidate."
    )
    data["strategy"].update({"worker_host": host, "worker_budget": {"max_runtime_seconds": 180}})
    data["workspace"]["promotion_mode"] = "artifact_only"
    spec = SearchSpec.model_validate(data)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    fixture = workspace / "search-fixture.json"
    fixture.write_text(json.dumps({
        "run_id": run_id, "frozen_spec_id": frozen.frozen_spec_id,
        "candidate_id": task.candidate_id, "spec": spec.model_dump(mode="json"),
    }))
    launch_instruction = (
        "Use search_start_agent_session, spawn_agent with its launch payload, then bind the native handle."
        if host == "codex" else
        "Use pi_search_pool_open for the existing candidate with final_verify=false."
    )
    prompt = (
        "Exercise the native Main/Search recovery workflow within 900 seconds. "
        f"The test controller already created exactly one standalone Search run in {fixture}. "
        "Read that file. Record high-confidence optimization triage; save a high-confidence "
        "spec draft with the exact supplied spec and no open questions, then link the existing "
        "run using goal_plus_link_search_run. Do NOT freeze/create/plan/materialize another run "
        "or candidate. Start its one worker and let it follow the frozen objective. "
        + launch_instruction + " Keep Main active while waiting; the controller will interrupt "
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
    if host == "codex":
        runner = CodexRunner(workspace, st_log_dir)
        initial_command = [*runner._build_base_cmd(), "--json", "$goal-plus " + prompt]
    else:
        pi = os.environ.get("ST_PI_BINARY", str(ROOT / ".goal-plus-dev/bin/pi-goal-plus"))
        pi_base = [pi, "--session-dir", str(workspace / "sessions")]
        if os.environ.get("ST_PI_MODEL"):
            pi_base += ["--model", os.environ["ST_PI_MODEL"]]
        initial_command = [*pi_base, "--session-id", "search-recovery-main", "-p", "--mode", "json", "/goal-plus " + prompt]
    initial_log = st_log_dir / f"search-recovery-{host}-initial.log"
    with initial_log.open("w") as log:
        process = subprocess.Popen(initial_command, cwd=workspace, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            def partial_ready():
                candidate = runtime._load_candidate_record(run_id, task.candidate_id)
                return (
                    len(candidate.iterations) == 1
                    and "RECOVERY_PARTIAL" in (task.workspace / "initial_program.py").read_text()
                    and list((runtime.root_dir / "host-recovery/workers").glob("*.json"))
                )
            _wait_for(process, partial_ready, initial_log)
        finally:
            _stop(process)
    if host == "pi-rpc":
        # A native Main interrupt alone leaves healthy Pi workers running.
        # This fixture explicitly owns and interrupts the sole pool as well.
        from goal_plus.pi_pool import close_pi_search_pool
        for pool in (runtime.root_dir / "host-pools/pi").glob("*/pool.json"):
            snapshot = close_pi_search_pool(root_dir=runtime.root_dir, pool_id=pool.parent.name,
                                            mode="interrupt", timeout_seconds=10, _allow_early_close=True)
            assert snapshot["active_count"] == 0
    goal_path, = (runtime.root_dir / "goal-plus").glob("gp_*/goal.json")
    before = json.loads(goal_path.read_text())
    session_id = before["active_session"]["session_id"]
    before_candidate = runtime._load_candidate_record(run_id, task.candidate_id)
    resume_prompt = "$goal-plus resume" if host == "codex" else "/goal-plus resume"
    if host == "codex":
        command = ["codex", "exec", "resume", "--skip-git-repo-check",
                   "--dangerously-bypass-hook-trust", "--dangerously-bypass-approvals-and-sandbox",
                   "--json", "-m", runner.model, session_id, resume_prompt]
    else:
        command = [*pi_base, "--session", session_id, "-p", "--mode", "json", resume_prompt]
    resumed_log = st_log_dir / f"search-recovery-{host}-resumed.log"
    with resumed_log.open("w") as log:
        process = subprocess.Popen(command, cwd=workspace, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            def recovered():
                candidate = runtime._load_candidate_record(run_id, task.candidate_id)
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
    after = json.loads(goal_path.read_text())
    assert after["goal_plus_id"] == before["goal_plus_id"]
    assert after["goal_revision"] == before["goal_revision"]
    assert after["outer_deadline_at"] == before["outer_deadline_at"]
    assert runtime.status(run_id).state == "promoted"
    assert after["host_command_invocations"][:-1] == before["host_command_invocations"]
    assert after["host_command_invocations"][-1]["action"] == "resume"
    assert after["active_session"]["session_id"] == session_id
    assert runtime.status(run_id).candidates_total == 1
    assert len(runtime._load_agent_sessions(run_id)) == 2  # One old worker, one replacement.
    assert after_candidate.iterations[0] == before_candidate.iterations[0]
    assert after_candidate.execution_generation == 1
    assert after_candidate.iterations[-1].agent_session_id != before_candidate.iterations[0].agent_session_id
    assert "RECOVERY_PARTIAL" not in (task.workspace / "initial_program.py").read_text()
