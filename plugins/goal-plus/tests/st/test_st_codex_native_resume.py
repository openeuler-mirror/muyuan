from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from tests.st.helpers.codex_runner import CodexRunner
from tests.st.hosts import ST_ACTIVE_ENV, link_host_assets


pytestmark = [pytest.mark.st, pytest.mark.st_codex]
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class InterruptedCodexSession:
    workspace: Path
    search_root: Path
    probe: Path
    runner: CodexRunner
    env: dict[str, str]
    goal_path: Path
    before: dict
    session_id: str


def _start_interrupted_codex_session(
    st_project_root: Path,
    st_log_dir: Path,
    *,
    scenario: str,
    interactive_initial: bool = False,
) -> InterruptedCodexSession:
    workspace = Path(tempfile.mkdtemp(prefix=f"{scenario}-", dir=st_project_root))
    link_host_assets(workspace, ROOT)
    search_root = workspace / ".gp"
    probe = workspace / "resume-probe.txt"
    runner = CodexRunner(project_root=workspace, log_dir=st_log_dir, default_timeout=300)
    env = {**os.environ, ST_ACTIVE_ENV: scenario}
    prompt = (
        "$goal-plus Within 300 seconds, read resume-probe.txt and prove its trimmed "
        "content is native-resume-ok. Do not edit files, run tests, enter Search, or "
        "start subagents. First record high-confidence Goal Mode triage. The test "
        "controller will create the file after interrupting and resuming this native "
        "session. Until it appears, keep the Goal active; do not mark it terminal or "
        "needs_user. After native resume, read it, audit the original goal, and call "
        "goal_plus_set_status(status=complete) with concrete evidence."
    )
    log_path = st_log_dir / f"{scenario}-initial.log"
    goal_path: Path | None = None
    if interactive_initial:
        goal_path = _interrupt_interactive_codex_turn(
            workspace=workspace,
            search_root=search_root,
            runner=runner,
            env=env,
            prompt=prompt,
            log_path=log_path,
        )
    else:
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
    return InterruptedCodexSession(
        workspace=workspace,
        search_root=search_root,
        probe=probe,
        runner=runner,
        env=env,
        goal_path=goal_path,
        before=before,
        session_id=session_id,
    )


def _interrupt_interactive_codex_turn(
    *,
    workspace: Path,
    search_root: Path,
    runner: CodexRunner,
    env: dict[str, str],
    prompt: str,
    log_path: Path,
) -> Path:
    if os.name != "posix":
        pytest.skip("interactive Codex interrupt ST requires a POSIX PTY")
    import fcntl
    import pty
    import select
    import struct
    import termios

    command = [
        "codex",
        "-C",
        str(workspace),
        "--no-alt-screen",
        "--dangerously-bypass-hook-trust",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if runner.model:
        command.extend(["-m", runner.model])
    command.append(prompt)

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    process = subprocess.Popen(
        command,
        cwd=workspace,
        env={**env, "TERM": "xterm-256color"},
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    goal_path: Path | None = None
    interrupt_sent = False
    try:
        with log_path.open("wb") as log:
            deadline = time.monotonic() + int(os.environ.get("ST_CODEX_TIMEOUT", "300"))
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        chunk = b""
                    if chunk:
                        log.write(chunk)
                        log.flush()
                paths = list((search_root / "goal-plus").glob("gp_*/goal.json"))
                if len(paths) == 1:
                    record = json.loads(paths[0].read_text(encoding="utf-8"))
                    if record.get("triage"):
                        goal_path = paths[0]
                        if not interrupt_sent:
                            # Codex 0.151's default interactive turn-interrupt key is Escape.
                            os.write(master, b"\x1b")
                            interrupt_sent = True
                        if record["active_session"]["state"] == "paused":
                            return goal_path
                if process.poll() is not None:
                    pytest.fail(
                        f"Initial Codex TUI exited with {process.returncode}; see {log_path}"
                    )
            pytest.fail(f"Codex TUI did not persist Interrupt; see {log_path}")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        os.close(master)


def _codex_interactive_command(
    session: InterruptedCodexSession,
    action: str,
) -> list[str]:
    command = [
        "codex",
        action,
        "-C",
        str(session.workspace),
        "--no-alt-screen",
        "--dangerously-bypass-hook-trust",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if session.runner.model:
        command.extend(["-m", session.runner.model])
    command.append(session.session_id)
    return command


def _run_codex_tui(
    command: list[str],
    *,
    session: InterruptedCodexSession,
    log_path: Path,
    done: Callable[[], bool],
    prompt: str | None = None,
) -> None:
    if os.name != "posix":
        pytest.skip("interactive Codex resume/fork ST requires a POSIX PTY")
    import fcntl
    import pty
    import select
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    process = subprocess.Popen(
        command,
        cwd=session.workspace,
        env={**session.env, "TERM": "xterm-256color"},
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    prompt_written = prompt is None
    prompt_submitted = prompt is None
    started_at = time.monotonic()
    try:
        with log_path.open("wb") as log:
            deadline = started_at + int(os.environ.get("ST_CODEX_TIMEOUT", "300"))
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        chunk = b""
                    if chunk:
                        log.write(chunk)
                        log.flush()
                elapsed = time.monotonic() - started_at
                if not prompt_written and elapsed >= 2:
                    os.write(master, prompt.encode("utf-8"))
                    prompt_written = True
                if prompt_written and not prompt_submitted and elapsed >= 3:
                    # Submit separately so Codex's burst-paste detector does not
                    # treat Enter as part of the pasted composer content.
                    os.write(master, b"\r")
                    prompt_submitted = True
                if done():
                    return
                if process.poll() is not None:
                    pytest.fail(f"Codex TUI exited with {process.returncode}; see {log_path}")
            pytest.fail(f"Codex TUI scenario timed out; see {log_path}")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        os.close(master)


def _assert_same_goal(before: dict, after: dict) -> None:
    assert after["goal_plus_id"] == before["goal_plus_id"]
    assert after["goal_revision"] == before["goal_revision"]
    assert after["host_command_invocations"] == before["host_command_invocations"]


def _codex_rollout_paths(env: dict[str, str]) -> set[Path]:
    codex_home = Path(env.get("CODEX_HOME", str(Path.home() / ".codex")))
    sessions_dir = codex_home / "sessions"
    return set(sessions_dir.rglob("rollout-*.jsonl")) if sessions_dir.is_dir() else set()


def _forked_rollout(
    paths_before: set[Path],
    session: InterruptedCodexSession,
    prompt: str,
) -> tuple[Path, dict] | None:
    for path in _codex_rollout_paths(session.env) - paths_before:
        try:
            with path.open(encoding="utf-8") as handle:
                first = json.loads(handle.readline())
            payload = first.get("payload", {})
            if (
                first.get("type") == "session_meta"
                and payload.get("cwd") == str(session.workspace)
                and payload.get("forked_from_id") == session.session_id
                and payload.get("id") != session.session_id
                and prompt in path.read_text(encoding="utf-8")
            ):
                return path, payload
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


def test_codex_exec_native_resume_after_interrupt(
    st_project_root: Path,
    st_log_dir: Path,
) -> None:
    session = _start_interrupted_codex_session(
        st_project_root,
        st_log_dir,
        scenario="codex_exec_native_resume",
    )
    session.probe.write_text("native-resume-ok\n", encoding="utf-8")
    resumed = subprocess.run(
        [
            *session.runner._build_base_cmd(),
            "--json", "resume", session.session_id,
            "Continue the active Goal Plus task in this same native session.",
        ],
        cwd=session.workspace,
        env=session.env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    resume_log = st_log_dir / "resumed.log"
    resume_log.write_text(resumed.stdout + resumed.stderr, encoding="utf-8")
    print(f"\n[codex_exec_native_resume] log: {resume_log}")
    assert resumed.returncode == 0, resume_log
    after = json.loads(session.goal_path.read_text(encoding="utf-8"))
    assert after["status"] == "complete", resume_log
    _assert_same_goal(session.before, after)
    assert after["active_session"]["session_id"] == session.session_id
    # Codex's terminal Stop hook releases the binding after completion.
    assert after["active_session"]["state"] == "detached"
    events = [
        json.loads(line)
        for line in session.goal_path.with_name("events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(event["event_type"] == "session_paused" for event in events)
    assert any(event["event_type"] == "session_reactivated" for event in events)


def test_codex_interactive_native_resume_after_interrupt(
    st_project_root: Path,
    st_log_dir: Path,
) -> None:
    session = _start_interrupted_codex_session(
        st_project_root,
        st_log_dir,
        scenario="codex_interactive_native_resume",
        interactive_initial=True,
    )
    session.probe.write_text("native-resume-ok\n", encoding="utf-8")
    log_path = st_log_dir / "interactive-resumed.log"
    _run_codex_tui(
        _codex_interactive_command(session, "resume"),
        session=session,
        log_path=log_path,
        prompt="Continue the active Goal Plus task in this same native session.",
        done=lambda: json.loads(session.goal_path.read_text(encoding="utf-8"))[
            "status"
        ]
        == "complete",
    )

    after = json.loads(session.goal_path.read_text(encoding="utf-8"))
    assert after["status"] == "complete", log_path
    _assert_same_goal(session.before, after)
    assert after["active_session"]["session_id"] == session.session_id


def test_codex_interactive_fork_does_not_inherit_goal_plus_binding(
    st_project_root: Path,
    st_log_dir: Path,
) -> None:
    session = _start_interrupted_codex_session(
        st_project_root,
        st_log_dir,
        scenario="codex_fork_session_isolation",
        interactive_initial=True,
    )
    paths_before = _codex_rollout_paths(session.env)
    fork_prompt = (
        "This is a fork isolation probe. Do not call tools or continue the inherited "
        "Goal Plus task; briefly acknowledge that this is a separate Codex session."
    )
    found: list[tuple[Path, dict]] = []

    def fork_started() -> bool:
        match = _forked_rollout(paths_before, session, fork_prompt)
        if match is None:
            return False
        found.append(match)
        return True

    _run_codex_tui(
        [*_codex_interactive_command(session, "fork"), fork_prompt],
        session=session,
        log_path=st_log_dir / "forked.log",
        done=fork_started,
    )

    assert found
    _fork_path, fork_meta = found[0]
    assert fork_meta["id"] != session.session_id
    assert fork_meta["forked_from_id"] == session.session_id
    after = json.loads(session.goal_path.read_text(encoding="utf-8"))
    assert after["status"] == "active"
    _assert_same_goal(session.before, after)
    assert after["active_session"]["session_id"] == session.session_id
    assert after["active_session"]["state"] == "paused"
