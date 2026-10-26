"""Recovery approval from fenced native session bindings, without PID inspection."""

from pathlib import Path

from goal_plus.domain.search import CandidateRecovery
from goal_plus.ports.scope import SCOPE_PROCESS_TERMINAL_STATUSES


def approve_recovery(root: Path, recovery: CandidateRecovery) -> None:
    from goal_plus.candidate_host import candidate_host_queries
    from goal_plus.host_scope import execution_scope_snapshot
    from goal_plus.infrastructure.state.files import write_json

    for session_id in recovery.fenced_session_ids:
        session = candidate_host_queries(root).source.read_agent_session(session_id)
        metadata = session.session_handle.metadata
        expected_transport = {"codex": "app-server", "pi": "native-rpc"}.get(session.agent_harness)
        scope_id = metadata.get("scope_id")
        if (
            expected_transport is None or metadata.get("transport") != expected_transport
            or not isinstance(scope_id, str) or not scope_id
        ):
            raise RuntimeError(f"RecoveryHostObservationMissing: {session_id}")
        scope = execution_scope_snapshot(root_dir=root, scope_id=scope_id)
        resources = [
            item for item in scope.get("processes", [])
            if item.get("label") == f"native-session:{session_id}"
            and item.get("kind", "candidate") == "candidate"
            and item.get("run_id") == session.run_id
        ]
        if scope.get("agent_harness") != session.agent_harness or not resources:
            raise RuntimeError(f"RecoveryHostObservationMissing: {session_id}")
        if any(item.get("status") not in SCOPE_PROCESS_TERMINAL_STATUSES for item in resources):
            raise RuntimeError(f"RecoverySessionNotReleased: {session_id}; release the registered session first")
    write_json(
        root / "host-recovery" / "approvals" / f"{recovery.recovery_id}.json",
        {"recovery": recovery.model_dump(mode="json", exclude={"state"})},
    )
