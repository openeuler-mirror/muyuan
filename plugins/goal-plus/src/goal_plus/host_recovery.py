"""Trusted host observations for Search recovery; never a model-supplied liveness flag.

The domain runtime only consumes approvals. Process inspection belongs here, not
in Search scheduling/settlement. Missing observations always fail closed.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Literal

from goal_plus.models import AgentSessionRecord, CandidateRecovery, SearchModel


class ProcessIdentity(SearchModel):
    pid: int
    started: str


class WorkerObservation(SearchModel):
    agent_session_id: str
    host: Literal["codex", "pi-rpc"]
    owner: ProcessIdentity
    state: Literal["running", "quiescent"] = "running"
    observed_at: str
    quiescent_recovery_id: str | None = None


def process_identity(pid: int) -> ProcessIdentity | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat=", "-o", "lstart="],
        capture_output=True, text=True, check=False, timeout=3,
    )
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return None
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("RecoveryProcessInspectionUnavailable")
    parts = result.stdout.strip().split(maxsplit=1)
    if parts[0].startswith("Z"):
        return None  # Exited, even if its parent has not reaped it yet.
    if len(parts) != 2:
        raise RuntimeError("RecoveryProcessInspectionUnavailable")
    return ProcessIdentity(pid=pid, started=parts[1].strip())


def codex_owner_pid() -> int | None:
    pid = os.getppid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=", "-o", "comm="],
            capture_output=True, text=True, check=False,
        )
        parts = result.stdout.strip().split(maxsplit=1)
        if result.returncode or len(parts) != 2:
            return None
        if Path(parts[1]).name == "codex":
            return pid
        pid = int(parts[0])
    return None


def claim_main_owner(root: Path, goal_id: str, host: str, session_id: str, pid: int, *, restoring: bool) -> None:
    """A native process claims its own Main binding; no background supervisor.

    Same-process reload/compaction is inert. A different live process cannot
    take over, and a replacement process records execution loss before any user
    input can accidentally reauthorize the task.
    """
    from goal_plus.goal_plus import FileGoalPlusRuntime
    from goal_plus.runtime import exclusive_file_lock, load_json, write_json

    if Path(goal_id).name != goal_id:
        raise ValueError("invalid Goal Plus identity")
    identity = process_identity(pid)
    if identity is None:
        raise RuntimeError("MainProcessUnavailable")
    path = root / "host-recovery" / "mains" / f"{goal_id}.json"
    runtime = FileGoalPlusRuntime(root)
    with exclusive_file_lock(path.with_suffix(".lock")):
        record = runtime.status(goal_id)
        binding = record.active_session
        if binding is None or binding.session_id != session_id or binding.host != host or binding.state in {"detached", "stale"}:
            raise RuntimeError("MainSessionMismatch: cannot claim another native session")
        previous = load_json(path) if path.exists() else None
        if previous is not None:
            old = ProcessIdentity.model_validate(previous["owner"])
            if old == identity:
                return
            if process_identity(old.pid) == old:
                raise RuntimeError("MainAlreadyRunning: the original native process still owns this task")
        if restoring and not runtime.is_terminal(record):
            runtime.handle_host_session_event(
                goal_id, host=host, session_id=session_id, event="execution_lost",
                delivery="next_user_turn", interaction="headless",
            )
        write_json(path, {"host": host, "session_id": session_id, "owner": identity.model_dump(mode="json")})


def _observation_path(root: Path, session_id: str) -> Path:
    if Path(session_id).name != session_id:
        raise ValueError("invalid agent session identity")
    return root / "host-recovery" / "workers" / f"{session_id}.json"


def interrupt_main_for_retry(root: Path, goal_id: str, expectation: dict, timeout_seconds: float = 5) -> dict:
    """Stop only a controller-owned, identity-checked Main; bounded host wait.

    Not registered as a model tool. The benchmark controller calls this before
    its known timeout, never to reinterpret an already ambiguous/user stop.
    """
    import signal
    import time
    from goal_plus.goal_plus import FileGoalPlusRuntime
    from goal_plus.runtime import exclusive_file_lock, load_json

    if Path(goal_id).name != goal_id or not 0 < timeout_seconds <= 30:
        raise ValueError("invalid host interruption identity or timeout")
    path = root / "host-recovery" / "mains" / f"{goal_id}.json"
    with exclusive_file_lock(path.with_suffix(".lock")):
        owner = load_json(path)
        identity = ProcessIdentity.model_validate(owner["owner"])
        if identity.pid <= 1 or identity.pid == os.getpid() or owner["session_id"] != expectation.get("session_id") or process_identity(identity.pid) != identity:
            raise RuntimeError("HostInterruptionRejected: owned Main process is unavailable")
        record = FileGoalPlusRuntime(root).stop_for_host_retry(goal_id, expectation)
        os.kill(identity.pid, signal.SIGTERM)
        until = time.monotonic() + timeout_seconds
        while process_identity(identity.pid) == identity and time.monotonic() < until:
            time.sleep(0.05)
        if process_identity(identity.pid) == identity:
            os.kill(identity.pid, signal.SIGKILL)
            until = time.monotonic() + timeout_seconds
            while process_identity(identity.pid) == identity and time.monotonic() < until:
                time.sleep(0.05)
        if process_identity(identity.pid) == identity:
            raise RuntimeError("HostInterruptionIncomplete: Main has not stopped")
        return {**expectation, "control_version": record.control.version}


def observe_worker(root: Path, session: AgentSessionRecord, pid: int) -> None:
    from goal_plus.runtime import utc_timestamp, write_json
    identity = process_identity(pid)
    if identity is None:
        raise RuntimeError("RecoveryWorkerProcessUnavailable")
    observation = WorkerObservation(
        agent_session_id=session.agent_session_id,
        host=session.host,
        owner=identity,
        observed_at=utc_timestamp(),
    )
    write_json(_observation_path(root, session.agent_session_id), observation.model_dump(mode="json"))


def observe_codex_registry(root: Path, sessions: list[AgentSessionRecord], payload: dict) -> None:
    """Accept only the native list_agents response delivered to PostToolUse."""
    from goal_plus.runtime import FileSearchRuntime, load_json, utc_timestamp, write_json
    agents = payload.get("agents")
    if not isinstance(agents, list):
        return
    for session in sessions:
        if session.host != "codex":
            continue
        path = _observation_path(root, session.agent_session_id)
        if not path.exists():
            continue
        observation = WorkerObservation.model_validate(load_json(path))
        for agent in agents:
            if not isinstance(agent, dict) or agent.get("agent_name") not in {
                session.host_handle.task_name, session.host_handle.external_id
            }:
                continue
            status = agent.get("agent_status")
            terminal = status in ("shutdown", "interrupted") or (
                isinstance(status, dict) and ("completed" in status or "errored" in status)
            )
            observation.state = "quiescent" if terminal else "running"
            observation.observed_at = utc_timestamp()
            record = FileSearchRuntime(root)._load_candidate_record(session.run_id, session.candidate_id)
            observation.quiescent_recovery_id = (
                record.recovery.recovery_id
                if terminal and record.recovery is not None
                and session.agent_session_id in record.recovery.fenced_session_ids
                else None
            )
            write_json(path, observation.model_dump(mode="json"))


def approve_recovery(root: Path, recovery: CandidateRecovery) -> None:
    from goal_plus.runtime import load_json, write_json
    for session_id in recovery.fenced_session_ids:
        path = _observation_path(root, session_id)
        if not path.exists():
            raise RuntimeError(f"RecoveryHostObservationMissing: {session_id}")
        observation = WorkerObservation.model_validate(load_json(path))
        identity = process_identity(observation.owner.pid)
        owner_exited = identity != observation.owner
        freshly_quiescent = (
            observation.state == "quiescent"
            and observation.quiescent_recovery_id == recovery.recovery_id
        )
        if not owner_exited and not freshly_quiescent:
            raise RuntimeError(f"RecoveryWorkerStillRunning: {session_id}; stop and observe the host worker first")
    write_json(
        root / "host-recovery" / "approvals" / f"{recovery.recovery_id}.json",
        {"recovery": recovery.model_dump(mode="json", exclude={"state"})},
    )
