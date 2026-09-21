"""Host-local ownership and cleanup for Goal Plus execution resources.

Execution scopes deliberately live outside Search records.  They bind live host
receipts to the native main-agent session without turning the Search runtime
into a worker supervisor.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from goal_plus.infrastructure.state.files import load_json
from goal_plus.bootstrap import (
    build_codex_scope_registry, build_scope_activation, build_scope_cleanup,
    build_scope_process_registry, build_scope_state,
)
from goal_plus.execution.scope import assert_scope_admission, newest_scope, normalize_native_agent_config, scope_attachments
from goal_plus.ports.scope import ScopeAttachment, ScopeHost, ScopeRecord, SCOPE_PROCESS_TERMINAL_STATUSES


def goal_scope_activation_id(record: Any) -> str:
    return f"{record.goal_plus_id}:{record.control.version}"


def goal_scope_resume_nonce(record: Any, scope: ScopeRecord | None) -> str | None:
    """Return a nonce only for an explicit resume after a closed scope."""
    if scope is None or scope.get("state") != "closed":
        return None
    invocation = next(reversed(record.host_command_invocations), None)
    active_session = getattr(record, "active_session", None)
    if (
        invocation is None
        or active_session is None
        or getattr(invocation, "action", None) != "resume"
        or getattr(invocation, "agent_harness", None) != scope.get("agent_harness")
        or getattr(invocation, "session_id", None) != getattr(active_session, "session_id", None)
    ):
        return None
    invocation_id = getattr(invocation, "invocation_id", None)
    return invocation_id if isinstance(invocation_id, str) and invocation_id else None


def ensure_execution_scope(
    *,
    root_dir: Path | str,
    agent_harness: ScopeHost,
    owner_session_id: str,
    run_id: str | None = None,
    native_agent_config: dict[str, Any] | None = None,
    activation_id: str | None = None,
    activation_nonce: str | None = None,
) -> dict[str, Any]:
    return build_scope_activation(root_dir).ensure(
        agent_harness=agent_harness, owner_session_id=owner_session_id,
        attachment=ScopeAttachment.from_id(run_id) if run_id is not None else None,
        native_agent_config=native_agent_config,
        activation_id=activation_id,
        activation_nonce=activation_nonce,
    )


def execution_scope_snapshot(
    *, root_dir: Path | str, scope_id: str
) -> dict[str, Any]:
    with build_scope_state(root_dir).lock(scope_id):
        return dict(build_scope_state(root_dir).read(scope_id))


def assert_execution_scope_open(
    *, root_dir: Path | str, scope_id: str, run_id: str
) -> dict[str, Any]:
    scope = execution_scope_snapshot(root_dir=root_dir, scope_id=scope_id)
    assert_scope_admission(scope, scope_id=scope_id, attachment=ScopeAttachment.from_id(run_id))
    return scope


@contextmanager
def execution_scope_admission(
    *, root_dir: Path | str, scope_id: str, run_id: str
) -> Iterator[dict[str, Any]]:
    """Hold launch admission until a new host resource is discoverable."""
    with build_scope_state(root_dir).lock(scope_id):
        scope = build_scope_state(root_dir).read(scope_id)
        assert_scope_admission(scope, scope_id=scope_id, attachment=ScopeAttachment.from_id(run_id))
        yield dict(scope)


@contextmanager
def internal_agent_run_admission(
    *, root_dir: Path | str, run_id: str
) -> Iterator[dict[str, Any] | None]:
    """Serialize internal-agent launch while allowing candidate-only fencing."""

    scope = execution_scope_for_run(root_dir=root_dir, run_id=run_id)
    if scope is None:
        yield None
        return
    scope_id = str(scope["scope_id"])
    with build_scope_state(root_dir).lock(scope_id):
        current = build_scope_state(root_dir).read(scope_id)
        assert_scope_admission(current, scope_id=scope_id, attachment=ScopeAttachment.from_id(run_id), internal=True)
        yield dict(current)


@contextmanager
def internal_agent_launch_registration(
    *, root_dir: Path | str, run_id: str, label: str,
) -> Iterator[Callable[[int], str] | None]:
    scope = execution_scope_for_run(root_dir=root_dir, run_id=run_id)
    if scope is None:
        yield None
        return
    with build_scope_process_registry(root_dir).internal_launch_registration(
        scope_id=str(scope["scope_id"]), attachment=ScopeAttachment.from_id(run_id), label=label,
    ) as register:
        yield register


def register_execution_process(
    *,
    root_dir: Path | str,
    scope_id: str,
    pid: int,
    label: str,
    run_id: str | None = None,
    kind: Literal["candidate", "internal"] = "candidate",
) -> str:
    return build_scope_process_registry(root_dir).register(
        scope_id=scope_id, pid=pid,
        label=label, attachment=ScopeAttachment.from_id(run_id) if run_id is not None else None, kind=kind,
    )


def finish_execution_process(
    *, root_dir: Path | str, scope_id: str, token: str
) -> None:
    return build_scope_process_registry(root_dir).finish(
        scope_id=scope_id, token=token,
    )


def register_codex_agent(
    *,
    root_dir: Path | str,
    scope_id: str,
    agent_id: str,
    label: str,
    run_id: str | None = None,
    tool_use_id: str | None = None,
    parent_session_id: str | None = None,
    candidate_id: str | None = None,
    agent_session_id: str | None = None,
    execution_generation: int | None = None,
) -> None:
    return build_codex_scope_registry(root_dir).register(
        scope_id=scope_id, agent_id=agent_id, label=label,
        attachment=ScopeAttachment.from_id(run_id) if run_id is not None else None,
        tool_use_id=tool_use_id, parent_session_id=parent_session_id,
        candidate_id=candidate_id,
        agent_session_id=agent_session_id,
        execution_generation=execution_generation,
    )


def complete_codex_spawn(
    *, root_dir: Path | str, scope_id: str, tool_use_id: str, agent_id: str,
) -> None:
    return build_codex_scope_registry(root_dir).complete_spawn(
        scope_id=scope_id, tool_use_id=tool_use_id, agent_id=agent_id,
    )


def finish_codex_agent(
    *,
    root_dir: Path | str,
    scope_id: str,
    agent_id: str,
    status: str = "finished",
    run_id: str | None = None,
    create_if_missing: bool = False,
    tool_use_id: str | None = None,
) -> bool:
    return build_codex_scope_registry(root_dir).finish(
        scope_id=scope_id, agent_id=agent_id, status=status,
        attachment=ScopeAttachment.from_id(run_id) if run_id is not None else None,
        create_if_missing=create_if_missing, tool_use_id=tool_use_id,
    )


def prepare_codex_agent_tool(
    *, root_dir: Path | str, scope_id: str,
    tool: Literal["followup_task", "interrupt_agent", "list_agents"],
    tool_use_id: str | None, target: str | None = None,
) -> None:
    return build_codex_scope_registry(root_dir).prepare_tool(
        scope_id=scope_id, tool=tool, tool_use_id=tool_use_id, target=target,
    )


def complete_codex_followup(
    *, root_dir: Path | str, scope_id: str, target: str, tool_use_id: str | None,
) -> None:
    return build_codex_scope_registry(root_dir).complete_followup(
        scope_id=scope_id, target=target, tool_use_id=tool_use_id,
    )


def record_codex_registry(
    *, root_dir: Path | str, scope_id: str, tool_use_id: str | None, payload: dict[str, Any],
) -> dict[str, Any]:
    return build_codex_scope_registry(root_dir).record_observation(
        scope_id=scope_id, tool_use_id=tool_use_id, payload=payload,
    )


def execution_scope_for_run(
    *, root_dir: Path | str, run_id: str
) -> dict[str, Any] | None:
    attachment = ScopeAttachment.from_id(run_id)
    return newest_scope(scope for scope in build_scope_state(root_dir).records() if attachment in scope_attachments(scope))


def pi_native_agent_config_for_run(
    *, root_dir: Path | str, run_id: str
) -> dict[str, str]:
    scope = execution_scope_for_run(root_dir=root_dir, run_id=run_id)
    if scope is None:
        raise RuntimeError(f"Search run {run_id} has no Pi execution scope")
    if scope.get("agent_harness") != "pi":
        raise RuntimeError(f"Search run {run_id} is not owned by a Pi execution scope")
    if scope.get("state") != "open":
        raise RuntimeError(f"Search run {run_id} Pi execution scope is not open")
    config = normalize_native_agent_config(
        "pi", scope.get("native_agent_config")
    )
    if config is None:
        raise RuntimeError(f"Search run {run_id} has no Pi native agent config")
    return config


def execution_scope_for_owner(
    *, root_dir: Path | str, agent_harness: ScopeHost, owner_session_id: str
) -> dict[str, Any] | None:
    return newest_scope(scope for scope in build_scope_state(root_dir).records() if (
        scope.get("agent_harness") == agent_harness and scope.get("owner_session_id") == owner_session_id
    ))


def execution_scope_for_codex_agent(
    *, root_dir: Path | str, agent_id: str, parent_session_id: str | None = None,
) -> dict[str, Any] | None:
    return build_codex_scope_registry(root_dir).for_agent(
        agent_id=agent_id, parent_session_id=parent_session_id,
    )


def execution_scope_for_codex_invocation(
    *, root_dir: Path | str, tool_use_id: str | None,
) -> dict[str, Any] | None:
    return build_codex_scope_registry(root_dir).for_invocation(
        tool_use_id=tool_use_id,
    )


def run_accepts_host_launch(*, root_dir: Path | str, run_id: str) -> bool:
    scope = execution_scope_for_run(root_dir=root_dir, run_id=run_id)
    return scope is None or (
        scope.get("state") == "open"
        and run_id not in scope.get("fenced_run_ids", [])
    )


def run_accepts_internal_agent_launch(
    *, root_dir: Path | str, run_id: str
) -> bool:
    scope = execution_scope_for_run(root_dir=root_dir, run_id=run_id)
    return scope is None or scope.get("state") == "open"


def _close_attachment_sessions(
    *, root_dir: Path | str, attachment: ScopeAttachment, scope_id: str, timeout_seconds: float
) -> tuple[list[dict[str, Any]], list[str]]:
    snapshots: list[dict[str, Any]] = []
    errors: list[str] = []
    resource_dir = Path(root_dir).resolve() / "host-scopes" / scope_id / "thinkthread-sessions"
    for path in resource_dir.glob("*.json"):
        record = load_json(path)
        if record.get("run_id") != attachment.identity:
            continue
        try:
            from goal_plus.infrastructure.thinkthread_sessions import release_session_resource

            snapshots.append(release_session_resource(path, timeout=timeout_seconds))
        except Exception as exc:
            errors.append(f"thinkthread-session:{path.stem}:{type(exc).__name__}:{exc}")
    return snapshots, errors


def close_run_internal_agents(
    *, root_dir: Path | str, run_id: str, timeout_seconds: float = 5.0,
) -> list[str]:
    """Host cleanup only; failed resources retain their recoverable handles."""
    errors = _close_run_internal_drainers(root_dir=root_dir, run_id=run_id, timeout_seconds=timeout_seconds)
    try:
        scope = execution_scope_for_run(root_dir=root_dir, run_id=run_id)
        if scope is not None:
            build_scope_process_registry(root_dir).release(
                scope_id=str(scope["scope_id"]), attachment=ScopeAttachment.from_id(run_id),
                kind="internal",
            )
    except Exception as exc:
        errors.append(f"internal:{type(exc).__name__}:{exc}")
    return errors


def _close_run_internal_drainers(
    *, root_dir: Path | str, run_id: str, timeout_seconds: float,
) -> list[str]:
    errors: list[str] = []
    try:
        from goal_plus.evidence_annotator import close_evidence_annotator

        close_evidence_annotator(
            root_dir=root_dir, run_id=run_id, timeout_seconds=timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must record host failures
        errors.append(f"annotation:{type(exc).__name__}:{exc}")
    try:
        from goal_plus.session_host import close_verifier_queue
        close_verifier_queue(root_dir=root_dir, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - cleanup must record host failures
        errors.append(f"llm-verifier:{type(exc).__name__}:{exc}")
    try:
        from goal_plus.thinkthread_annotation import close_thinkthread_annotation

        state = close_thinkthread_annotation(
            root_dir=root_dir, run_id=run_id, timeout_seconds=timeout_seconds
        )
        if isinstance(state, dict) and state.get("status") != "closed":
            errors.append(
                "thinkthread-annotation:cleanup_incomplete:"
                f"{state.get('status')}"
            )
    except Exception as exc:  # noqa: BLE001 - cleanup must record host failures
        # A run without a TT annotation service is a normal case.  The close
        # implementation returns None, but provider preflight may be absent.
        state_path = (
            Path(root_dir).expanduser().resolve()
            / "host-pools"
            / "thinkthread-annotations"
            / run_id
            / "state.json"
        )
        if state_path.exists():
            errors.append(f"thinkthread-annotation:{type(exc).__name__}:{exc}")
    return errors


@dataclass(frozen=True)
class _HostScopeCleanupServices:
    root_dir: Path | str

    def close_sessions(
        self, *, attachment: ScopeAttachment, scope_id: str, timeout_seconds: float,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        return _close_attachment_sessions(
            root_dir=self.root_dir, attachment=attachment, scope_id=scope_id, timeout_seconds=timeout_seconds,
        )

    def close_drainers(self, *, run_id: str, timeout_seconds: float) -> list[str]:
        return _close_run_internal_drainers(
            root_dir=self.root_dir, run_id=run_id, timeout_seconds=timeout_seconds,
        )

    def codex_run_is_quiescent(self, run_id: str, *, scope: ScopeRecord) -> bool:
        return _codex_run_is_durably_quiescent(self.root_dir, run_id, scope=scope)


def close_execution_scope(
    *,
    root_dir: Path | str,
    scope_id: str,
    reason: str,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    return build_scope_cleanup(root_dir, _HostScopeCleanupServices(root_dir)).close(
        scope_id=scope_id, reason=reason, timeout_seconds=timeout_seconds,
    )


def close_run_candidates(
    *, root_dir: Path | str, scope_id: str, run_id: str, timeout_seconds: float = 5.0
) -> dict[str, Any]:
    return build_scope_cleanup(root_dir, _HostScopeCleanupServices(root_dir)).close_candidates(
        scope_id=scope_id, attachment=ScopeAttachment.from_id(run_id), timeout_seconds=timeout_seconds,
    )


def _codex_run_is_durably_quiescent(root_dir: Path | str, run_id: str, *, scope: ScopeRecord) -> bool:
    from goal_plus.candidate_host import candidate_host_queries

    queries = candidate_host_queries(root_dir)
    sessions = queries.sessions(run_id)
    if not sessions:
        return True
    for session in sessions:
        if session.agent_harness != "codex":
            continue
        metadata = session.session_handle.metadata
        recovery = queries.recovery(run_id, session.candidate_id)
        if (
            recovery is not None and recovery.state == "restored"
            and session.execution_generation < recovery.generation
            and session.agent_session_id in recovery.fenced_session_ids
        ):
            # Restore already required trusted quiescence for these exact sessions.
            continue
        if metadata.get("transport") == "app-server":
            # A released receipt closes admission for this exact session.
            # It makes no claim about operating-system process lifetime.
            resources = [item for item in scope.get("processes", [])
                         if item.get("run_id") == run_id
                         and item.get("kind", "candidate") == "candidate"
                         and item.get("label") == f"native-session:{session.agent_session_id}"]
            if resources and all(item.get("status") in SCOPE_PROCESS_TERMINAL_STATUSES for item in resources):
                continue
            return False
        return False
    return True
