from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.st.helpers.codex_runner import CodexRunner
from tests.st.hosts import ST_ACTIVE_ENV, link_host_assets


pytestmark = [pytest.mark.st, pytest.mark.st_codex]
ROOT = Path(__file__).resolve().parents[2]


def test_codex_exec_native_resume_after_interrupt(
    st_project_root: Path,
    st_log_dir: Path,
) -> None:
    workspace = Path(tempfile.mkdtemp(prefix="native-resume-", dir=st_project_root))
    link_host_assets(workspace, ROOT)
    search_root = workspace / ".gp"
    probe = workspace / "resume-probe.txt"
    runner = CodexRunner(project_root=workspace, log_dir=st_log_dir, default_timeout=300)
    env = {**os.environ, ST_ACTIVE_ENV: "codex_exec_native_resume"}
    prompt = (
        "$goal-plus Within 300 seconds, read resume-probe.txt and prove its trimmed "
        "content is native-resume-ok. Do not edit files, run tests, enter Search, or "
        "start subagents. First record high-confidence Goal Mode triage. The test "
        "controller will create the file after interrupting and resuming this native "
        "session. Until it appears, keep the Goal active; do not mark it terminal or "
        "needs_user. After native resume, read it, audit the original goal, and call "
        "goal_plus_set_status(status=complete) with concrete evidence."
    )
    log_path = st_log_dir / "initial.log"
    goal_path: Path | None = None
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [*runner._build_base_cmd(), "--json", prompt],
            cwd=workspace,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path.read_text(encoding="utf-8")[-4000:]
                paths = list((search_root / "goal-plus").glob("gp_*/goal.json"))
                if len(paths) == 1:
                    record = json.loads(paths[0].read_text(encoding="utf-8"))
                    if record.get("triage"):
                        goal_path = paths[0]
                        break
                time.sleep(0.1)
            assert goal_path is not None, f"No durable triage; see {log_path}"
            process.send_signal(signal.SIGINT)
            process.wait(timeout=30)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)

    before = json.loads(goal_path.read_text(encoding="utf-8"))
    assert before["status"] == "active", log_path
    assert before["active_session"]["state"] == "paused", log_path
    session_id = before["active_session"]["session_id"]
    probe.write_text("native-resume-ok\n", encoding="utf-8")
    resumed = subprocess.run(
        [
            *runner._build_base_cmd(),
            "--json", "resume", session_id,
            "Continue the active Goal Plus task in this same native session.",
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    resume_log = st_log_dir / "resumed.log"
    resume_log.write_text(resumed.stdout + resumed.stderr, encoding="utf-8")
    print(f"\n[codex_exec_native_resume] logs: {log_path}, {resume_log}")
    assert resumed.returncode == 0, resume_log
    after = json.loads(goal_path.read_text(encoding="utf-8"))
    assert after["status"] == "complete", resume_log
    assert after["goal_plus_id"] == before["goal_plus_id"]
    assert after["goal_revision"] == before["goal_revision"]
    assert after["host_command_invocations"] == before["host_command_invocations"]
    assert after["active_session"]["session_id"] == session_id
    # Codex's terminal Stop hook releases the binding after completion.
    assert after["active_session"]["state"] == "detached"
    events = [
        json.loads(line)
        for line in goal_path.with_name("events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event_type"] == "session_paused" for event in events)
    assert any(event["event_type"] == "session_reactivated" for event in events)
