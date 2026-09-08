"""Pi pool launch/cleanup boundaries, without native inference."""

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus import pi_pool
from goal_plus.execution import process_identity as inspection
from goal_plus.execution.process_resources import OwnedProcess
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json
from goal_plus.ports.process_identity import ProcessSnapshot, ProcessWitness
from tests.test_pi_driver import _make_project, _pi_rpc_spec_with_budget
from tests.test_pi_pool import _planned_candidates
from goal_plus.runtime import FileSearchRuntime

pytestmark = pytest.mark.pi


@pytest.fixture
def pool_job(tmp_path):
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1), [project / "evaluator.py"], native_host="pi",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    pool = pi_pool.open_pi_search_pool(root_dir=runtime.root_dir, run_id=run_id)
    submitted = pi_pool._submit_pi_search_pool(
        root_dir=runtime.root_dir, pool_id=pool["pool_id"], candidate_id=candidate_id,
        _launcher=lambda **_: os.getpid(),
    )
    return runtime.root_dir, pool["pool_id"], submitted["job_id"]


@pytest.mark.linux_procfs
def test_pool_rejects_a_live_unrelated_pid_without_signalling(pool_job, monkeypatch):
    root, pool_id, job_id = pool_job
    kill = os.kill

    def no_signal(pid, sig):
        assert sig == 0, "an unrelated process was signalled"
        return kill(pid, sig)

    monkeypatch.setattr(os, "kill", no_signal)
    result = pi_pool.close_pi_search_pool(
        root_dir=root, pool_id=pool_id, mode="interrupt", timeout_seconds=0,
        _allow_early_close=True,
    )
    assert result["state"] == "closed"
    assert result["jobs"][0]["job_id"] == job_id
    assert result["jobs"][0]["error"]["error_type"] == "WorkerProcessExited"


@pytest.mark.parametrize("field,value", [
    ("status", "starting"), ("status", "completed"), ("pid", None), ("pid", 999999),
])
def test_worker_cannot_execute_without_its_committed_launch(pool_job, monkeypatch, field, value):
    root, pool_id, job_id = pool_job
    job = pi_pool._load_job(root, pool_id, job_id)
    job[field] = value
    pi_pool._write_job(root, pool_id, job)
    before = pi_pool._job_path(root, pool_id, job_id).read_bytes()
    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", lambda **_: pytest.fail("unclaimed worker executed"))
    assert pi_pool.run_pool_worker(root_dir=root, pool_id=pool_id, job_id=job_id) == 1
    assert pi_pool._job_path(root, pool_id, job_id).read_bytes() == before


@pytest.mark.parametrize("state", ["interrupting", "closed"])
def test_worker_cannot_start_after_pool_revocation(pool_job, monkeypatch, state):
    root, pool_id, job_id = pool_job
    pool = pi_pool._load_pool(root, pool_id)
    pool["state"] = state
    pi_pool._write_pool(root, pool)
    before = pi_pool._job_path(root, pool_id, job_id).read_bytes()
    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", lambda **_: pytest.fail("fenced worker executed"))
    assert pi_pool.run_pool_worker(root_dir=root, pool_id=pool_id, job_id=job_id) == 1
    assert pi_pool._job_path(root, pool_id, job_id).read_bytes() == before


def test_worker_waits_for_launch_commit_before_reading_request(pool_job, monkeypatch):
    root, pool_id, job_id = pool_job
    entered = threading.Event()
    monkeypatch.setattr(pi_pool, "load_json", lambda path: (entered.set(), load_json(path))[1])
    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", lambda **_: pytest.fail("unclaimed worker executed"))
    result = []
    # signal handlers remain on the real main thread in production.
    monkeypatch.setattr(pi_pool.signal, "signal", lambda *_: None)
    with exclusive_file_lock(pi_pool._pool_lock_path(root, pool_id)):
        job = pi_pool._load_job(root, pool_id, job_id)
        job["status"] = "starting"
        pi_pool._write_job(root, pool_id, job)
        entered.clear()
        thread = threading.Thread(target=lambda: result.append(pi_pool.run_pool_worker(
            root_dir=root, pool_id=pool_id, job_id=job_id,
        )))
        thread.start()
        read_while_locked = entered.wait(0.1)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not read_while_locked
    assert result == [1]


def test_pool_observation_unknown_preserves_job_and_fences_close(pool_job, monkeypatch):
    root, pool_id, job_id = pool_job
    before = pi_pool._job_path(root, pool_id, job_id).read_bytes()

    def unknown(*_, **__):
        raise inspection.ProcessInspectionUnavailable("ProcessInspectionUnavailable: test")

    monkeypatch.setattr(pi_pool, "_job_process", unknown, raising=False)
    with pytest.raises(inspection.ProcessInspectionUnavailable):
        pi_pool.snapshot_pi_search_pool(root_dir=root, pool_id=pool_id)
    assert pi_pool._job_path(root, pool_id, job_id).read_bytes() == before
    with pytest.raises(inspection.ProcessInspectionUnavailable):
        pi_pool.close_pi_search_pool(
            root_dir=root, pool_id=pool_id, mode="interrupt", timeout_seconds=0,
            _allow_early_close=True,
        )
    assert pi_pool._load_pool(root, pool_id)["state"] == "interrupting"
    assert pi_pool._job_path(root, pool_id, job_id).read_bytes() == before


@pytest.mark.linux_procfs
@pytest.mark.parametrize("mode", ["drain", "interrupt"])
def test_terminal_job_does_not_prove_resource_exit(pool_job, monkeypatch, mode):
    root, pool_id, job_id = pool_job
    monkeypatch.setattr(pi_pool, "utc_timestamp", lambda: "2026-09-06T06:09:35Z")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        snapshot = inspection.inspect_process(process.pid)
        resource = OwnedProcess(snapshot.pid, snapshot.start_token, snapshot.process_group_id)
        job = pi_pool._load_job(root, pool_id, job_id)
        job.update(status="completed", pid=process.pid)
        pi_pool._write_job(root, pool_id, job)
        before = pi_pool._load_job(root, pool_id, job_id)
        monkeypatch.setattr(pi_pool, "_job_process", lambda *_: resource, raising=False)
        result = pi_pool.close_pi_search_pool(
            root_dir=root, pool_id=pool_id, mode=mode, timeout_seconds=0,
        )
        if mode == "drain":
            assert result["state"] == "draining" and result["close_timed_out"]
            assert process.poll() is None
        else:
            assert result["state"] == "closed"
            assert process.wait(timeout=5) in {-signal.SIGTERM, -signal.SIGKILL}
        after = pi_pool._load_job(root, pool_id, job_id)
        witness = ProcessWitness.model_validate(after.pop("witness"))
        assert witness.members[process.pid] == snapshot.start_token
        assert after == before
        assert "witness" not in result["jobs"][0]
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("changed", [None, "root", "pool", "job", "extra"])
def test_pool_inspects_exact_launch_arguments(pool_job, monkeypatch, changed):
    root, pool_id, job_id = pool_job
    pid = os.getpid()
    command = [sys.executable, "-m", "goal_plus.pi_pool", "worker", "--root", str(root),
               "--pool-id", pool_id, "--job-id", job_id]
    if changed == "extra":
        command += ["--job-id", job_id]
    elif changed:
        command[{"root": 5, "pool": 7, "job": 9}[changed]] += "_other"
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: (
        b"\0".join(os.fsencode(arg) for arg in command) + b"\0"
        if path == Path(f"/proc/{pid}/cmdline") else original(path)
    ))
    monkeypatch.setattr(inspection, "inspect_process", lambda _: ProcessSnapshot(pid, "token", pid, "S"))
    process = pi_pool._job_process(root, pool_id, pi_pool._load_job(root, pool_id, job_id))
    if changed:
        assert process is None
    else:
        assert (process.pid, process.start_token, process.process_group_id) == (pid, "token", pid)


@pytest.mark.parametrize("field,value", [
    ("root_dir", "/other"), ("run_id", "other"), ("candidate_id", "other"),
    ("execution_scope_id", "scope_other"),
])
def test_worker_rejects_a_request_outside_its_job_binding(pool_job, monkeypatch, field, value):
    root, pool_id, job_id = pool_job
    request_path = pi_pool._job_dir(root, pool_id, job_id) / "request.json"
    request = load_json(request_path)
    request[field] = value
    write_json(request_path, request)
    before = pi_pool._job_path(root, pool_id, job_id).read_bytes()
    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", lambda **_: pytest.fail("mismatched request executed"))
    assert pi_pool.run_pool_worker(root_dir=root, pool_id=pool_id, job_id=job_id) == 1
    assert pi_pool._job_path(root, pool_id, job_id).read_bytes() == before


@pytest.mark.linux_procfs
@pytest.mark.parametrize("after_write", [False, True])
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_launch_publication_failure_cannot_start_candidate(pool_job, monkeypatch, tmp_path, after_write, error):
    root, original_pool_id, original_job_id = pool_job
    original_job = pi_pool._load_job(root, original_pool_id, original_job_id)
    pool_id = pi_pool.open_pi_search_pool(root_dir=root, run_id=original_job["run_id"])["pool_id"]
    original_write = pi_pool._write_job
    popen = subprocess.Popen
    processes = []
    marker = tmp_path / "candidate-called"

    def launch(command, **options):
        # The real pool API executes in a local child; a hard guard prevents native inference.
        code = (
            "from pathlib import Path\nfrom goal_plus import pi_pool\n"
            "def forbidden(**kwargs):\n"
            f"    Path({str(marker)!r}).touch()\n"
            "    raise RuntimeError('native inference disabled')\n"
            "pi_pool.run_pi_search_candidate = forbidden\n"
            f"raise SystemExit(pi_pool.run_pool_worker(root_dir={str(root)!r}, "
            f"pool_id={pool_id!r}, job_id={command[-1]!r}))\n"
        )
        process = popen([sys.executable, "-c", code], **options)
        processes.append(process)
        return process

    def write_job(root, pool, job):
        if job["status"] == "running":
            if after_write:
                original_write(root, pool, job)
            raise error("PID publication interrupted")
        original_write(root, pool, job)

    monkeypatch.setattr(pi_pool.subprocess, "Popen", launch)
    monkeypatch.setattr(pi_pool, "_write_job", write_job)
    try:
        with pytest.raises(error, match="PID publication interrupted"):
            pi_pool._submit_pi_search_pool(root_dir=root, pool_id=pool_id, candidate_id=original_job["candidate_id"])
        assert len(processes) == 1
        assert processes[0].wait(timeout=10) == 1
        assert not marker.exists()
        job_id = pi_pool._load_pool(root, pool_id)["jobs"][0]
        job = pi_pool._load_job(root, pool_id, job_id)
        assert job["pid"] == processes[0].pid and job["status"] == "failed"
        assert job["error"]["stage"] == "launch"
        assert not (pi_pool._job_dir(root, pool_id, job_id) / "result.json").exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_pool_launch_stream_close_failure_reaps_its_child(pool_job, monkeypatch, error):
    root, pool_id, job_id = pool_job
    original_open, popen = Path.open, subprocess.Popen
    processes = []

    @contextmanager
    def output(path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as stream:
            yield stream
        raise error("log close failed")

    def launch(_command, **options):
        process = popen([sys.executable, "-c", "import time; time.sleep(60)"], **options)
        processes.append(process)
        return process

    monkeypatch.setattr(Path, "open", lambda path, *a, **kw: (
        output(path, *a, **kw) if path.name == "stderr.log" else original_open(path, *a, **kw)
    ))
    monkeypatch.setattr(pi_pool.subprocess, "Popen", launch)
    try:
        with exclusive_file_lock(pi_pool._pool_lock_path(root, pool_id)), pytest.raises(error, match="log close failed"):
            pi_pool._launch_pool_job(root_dir=root, pool_id=pool_id, job_id=job_id)
        assert len(processes) == 1 and processes[0].returncode is not None
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.linux_procfs
def test_dead_job_does_not_keep_its_minimum_lease(pool_job, monkeypatch):
    root, pool_id, _ = pool_job
    monkeypatch.setenv("GOAL_PLUS_OUTER_DEADLINE_AT", str(time.time() + 1000))
    result = pi_pool.close_pi_search_pool(root_dir=root, pool_id=pool_id, timeout_seconds=0)
    assert result["state"] == "closed" and result["active_count"] == 0


def test_drain_cannot_weaken_an_existing_interrupt_fence(pool_job, monkeypatch):
    root, pool_id, _ = pool_job
    pool = pi_pool._load_pool(root, pool_id)
    pool["state"] = "interrupting"
    pi_pool._write_pool(root, pool)
    monkeypatch.setattr(pi_pool, "_job_process", lambda *_: SimpleNamespace(running=lambda: True))
    result = pi_pool.close_pi_search_pool(
        root_dir=root, pool_id=pool_id, mode="drain", timeout_seconds=0, _allow_early_close=True,
    )
    assert result["state"] == "interrupting" and result["close_timed_out"]


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_pool_rejects_unbounded_close_before_writing_a_fence(pool_job, timeout):
    root, pool_id, _ = pool_job
    before = pi_pool._pool_path(root, pool_id).read_bytes()
    with pytest.raises(ValueError, match="timeout_seconds"):
        pi_pool.close_pi_search_pool(root_dir=root, pool_id=pool_id, timeout_seconds=timeout)
    assert pi_pool._pool_path(root, pool_id).read_bytes() == before


@pytest.mark.linux_procfs
def test_unknown_job_does_not_prevent_stopping_other_owned_resources(pool_job, monkeypatch):
    root, pool_id, job_id = pool_job
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        snapshot = inspection.inspect_process(process.pid)
        resource = OwnedProcess(snapshot.pid, snapshot.start_token, snapshot.process_group_id)
        job = pi_pool._load_job(root, pool_id, job_id)
        job.update(status="completed", pid=process.pid)
        pi_pool._write_job(root, pool_id, job)
        unknown_job = {**job, "job_id": "job_unknown", "pid": 999999}
        pi_pool._write_job(root, pool_id, unknown_job)
        pool = pi_pool._load_pool(root, pool_id)
        pool["jobs"].insert(0, "job_unknown")
        pi_pool._write_pool(root, pool)

        def inspect(_root, _pool, job):
            if job["job_id"] == "job_unknown":
                raise inspection.ProcessInspectionUnavailable("unreadable identity")
            return resource

        monkeypatch.setattr(pi_pool, "_job_process", inspect)
        with pytest.raises(inspection.ProcessInspectionUnavailable, match="unreadable identity"):
            pi_pool.close_pi_search_pool(root_dir=root, pool_id=pool_id, mode="interrupt", timeout_seconds=0)
        assert process.wait(timeout=5) in {-signal.SIGTERM, -signal.SIGKILL}
        assert pi_pool._load_pool(root, pool_id)["state"] == "interrupting"
        assert pi_pool._load_job(root, pool_id, "job_unknown")["pid"] == 999999
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_initial_pool_open_cleans_up_after_launch_interrupt(pool_job, monkeypatch):
    root, original_pool_id, original_job_id = pool_job
    job = pi_pool._load_job(root, original_pool_id, original_job_id)
    launched = []

    def interrupted(**kwargs):
        launched.append(kwargs["pool_id"])
        raise KeyboardInterrupt("launch interrupted")

    monkeypatch.setattr(pi_pool, "_launch_pool_job", interrupted)
    with pytest.raises(KeyboardInterrupt, match="launch interrupted"):
        pi_pool.open_pi_search_pool(root_dir=root, run_id=job["run_id"], candidate_ids=[job["candidate_id"]])
    assert len(launched) == 1
    assert pi_pool._load_pool(root, launched[0])["state"] == "closed"


@pytest.mark.linux_procfs
def test_actual_pool_command_is_identifiable_before_claim(pool_job, monkeypatch):
    root, pool_id, _ = pool_job
    job_id = "job_unregistered"
    # Missing request and pool membership prevent native inference after registration.
    pi_pool._job_dir(root, pool_id, job_id).mkdir()
    pi_pool._write_job(root, pool_id, {"job_id": job_id, "status": "starting", "pid": None})
    popen, processes = subprocess.Popen, []

    def capture(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(pi_pool.subprocess, "Popen", capture)
    try:
        with exclusive_file_lock(pi_pool._pool_lock_path(root, pool_id)):
            pid = pi_pool._launch_pool_job(root_dir=root, pool_id=pool_id, job_id=job_id)
            job = pi_pool._load_job(root, pool_id, job_id)
            assert job["pid"] == pid and job["witness"]
            def exec_window(*_):
                raise inspection.ProcessInspectionUnavailable("cmdline during exec")

            monkeypatch.setattr(inspection, "inspect_command", exec_window)
            resource = pi_pool._job_process(root, pool_id, job)
            assert resource.pid == pid and resource.process_group_id == pid
            assert resource.running()
        assert processes[0].wait(timeout=10) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.linux_procfs
def test_orphaned_pool_group_is_unknown_until_its_members_exit(pool_job):
    root, pool_id, job_id = pool_job
    leader = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], process_group=0)
    member = None
    try:
        member = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], process_group=leader.pid)
        identity = inspection.inspect_process(leader.pid)
        resource = OwnedProcess(leader.pid, identity.start_token, leader.pid)
        assert resource.running()
        witness = resource.witness
        assert member.pid in witness.members
        job = pi_pool._load_job(root, pool_id, job_id)
        job.update(pid=leader.pid, status="completed")
        pi_pool._write_job(root, pool_id, job)
        before = pi_pool._job_path(root, pool_id, job_id).read_bytes()
        leader.terminate()
        leader.wait(timeout=5)
        with pytest.raises(inspection.ProcessInspectionUnavailable, match="group_ownership_unknown"):
            pi_pool.close_pi_search_pool(root_dir=root, pool_id=pool_id, mode="interrupt", timeout_seconds=0)
        assert member.poll() is None
        assert pi_pool._load_pool(root, pool_id)["state"] == "interrupting"
        assert pi_pool._job_path(root, pool_id, job_id).read_bytes() == before
        pi_pool._remember_job_process(root, pool_id, job_id, witness)
        assert pi_pool.close_pi_search_pool(
            root_dir=root, pool_id=pool_id, mode="interrupt", timeout_seconds=0,
        )["state"] == "closed"
        assert member.wait(timeout=5) in {-signal.SIGTERM, -signal.SIGKILL}
    finally:
        for process in (member, leader):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
