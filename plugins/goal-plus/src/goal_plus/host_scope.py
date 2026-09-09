"""Host-local ownership and cleanup for Goal Plus execution resources.

Execution scopes deliberately live outside Search records.  They bind live host
resources to the native main-agent process without turning the Search runtime
into a worker supervisor.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from goal_plus.infrastructure.state.files import load_json
from goal_plus.bootstrap import (
    build_codex_scope_registry, build_scope_activation, build_scope_cleanup,
    build_scope_guardian_launcher, build_scope_process_registry, build_scope_state,
)
from goal_plus.execution.scope import assert_scope_admission, newest_scope, normalize_native_agent_config, scope_attachments
from goal_plus.execution.process_identity import process_alive
from goal_plus.execution.scope_guardian import ScopeGuardian
from goal_plus.ports.scope import ScopeAttachment, ScopeGuardianObservation, ScopeHost, validate_scope_id
from goal_plus.ports.process_identity import ProcessWitness

def ensure_execution_scope(
    *,
    root_dir: Path | str,
    host: ScopeHost,
    owner_session_id: str,
    owner_pid: int,
    run_id: str | None = None,
    native_agent_config: dict[str, Any] | None = None,
    start_guardian: bool = True,
    activation_id: str | None = None,
) -> dict[str, Any]:
    return build_scope_activation(root_dir, _HostScopeCleanupServices(root_dir)).ensure(
        host=host, owner_session_id=owner_session_id, owner_pid=owner_pid,
        attachment=ScopeAttachment.from_id(run_id) if run_id is not None else None,
        native_agent_config=native_agent_config,
        start_guardian=start_guardian, activation_id=activation_id,
    )


def restore_execution_scope_guardians(
    *, root_dir: Path | str, host: ScopeHost, owner_session_id: str,
) -> None:
    return build_scope_guardian_launcher(root_dir).restore_owner(
        host=host, owner_session_id=owner_session_id,
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
def host_run_admission(
    *, root_dir: Path | str, run_id: str
) -> Iterator[dict[str, Any] | None]:
    """Serialize an optional run-scoped launch against scope fencing."""
    scope = execution_scope_for_run(root_dir=root_dir, run_id=run_id)
    if scope is None:
        yield None
        return
    with execution_scope_admission(
        root_dir=root_dir,
        scope_id=str(scope["scope_id"]),
        run_id=run_id,
    ) as admitted:
        yield admitted


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
    process_group_id: int | None,
    label: str,
    run_id: str | None = None,
    kind: Literal["candidate", "internal"] = "candidate",
) -> str:
    return build_scope_process_registry(root_dir).register(
        scope_id=scope_id, pid=pid, process_group_id=process_group_id,
        label=label, attachment=ScopeAttachment.from_id(run_id) if run_id is not None else None, kind=kind,
    )


def remember_execution_process(
    *, root_dir: Path | str, scope_id: str, token: str, witness: ProcessWitness,
) -> None:
    return build_scope_process_registry(root_dir).remember(
        scope_id=scope_id, token=token, witness=witness,
    )


def execution_process_witness(*, root_dir: Path | str, scope_id: str, token: str) -> ProcessWitness | None:
    return build_scope_process_registry(root_dir).witness(
        scope_id=scope_id, token=token,
    )


def observe_execution_processes(*, root_dir: Path | str, scope_id: str) -> None:
    return build_scope_process_registry(root_dir).observe(
        scope_id=scope_id,
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
) -> None:
    return build_codex_scope_registry(root_dir).register(
        scope_id=scope_id, agent_id=agent_id, label=label,
        attachment=ScopeAttachment.from_id(run_id) if run_id is not None else None,
        tool_use_id=tool_use_id, parent_session_id=parent_session_id,
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
    if scope.get("host") != "pi":
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
    *, root_dir: Path | str, host: ScopeHost, owner_session_id: str
) -> dict[str, Any] | None:
    return newest_scope(scope for scope in build_scope_state(root_dir).records() if (
        scope.get("host") == host and scope.get("owner_session_id") == owner_session_id
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


def _pool_paths(root_dir: Path | str, host_dir: str) -> list[Path]:
    root = Path(root_dir).expanduser().resolve() / "host-pools" / host_dir
    return sorted(root.glob("*/pool.json"))


def _close_attachment_pools(
    *, root_dir: Path | str, attachment: ScopeAttachment, scope_id: str, timeout_seconds: float
) -> tuple[list[dict[str, Any]], list[str]]:
    snapshots: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in _pool_paths(root_dir, "pi"):
        pool = load_json(path)
        if pool.get("run_id") != attachment.identity or pool.get("execution_scope_id") != scope_id:
            continue
        try:
            from goal_plus.pi_pool import close_pi_search_pool

            snapshot = close_pi_search_pool(
                root_dir=root_dir,
                pool_id=str(pool["pool_id"]),
                mode="interrupt",
                timeout_seconds=timeout_seconds,
                _allow_early_close=True,
            )
            snapshots.append(snapshot)
            if snapshot.get("state") != "closed":
                errors.append(
                    f"pi:{pool.get('pool_id')}:cleanup_incomplete:{snapshot.get('state')}"
                )
        except Exception as exc:  # noqa: BLE001 - cleanup must record host failures
            errors.append(f"pi:{pool.get('pool_id')}:{type(exc).__name__}:{exc}")
    for path in _pool_paths(root_dir, "thinkthread"):
        pool = load_json(path)
        if pool.get("run_id") != attachment.identity or pool.get("execution_scope_id") != scope_id:
            continue
        try:
            from goal_plus.thinkthread_pool import close_pool

            snapshot = close_pool(
                root_dir=root_dir,
                pool_id=str(pool["pool_id"]),
                mode="interrupt",
                timeout_seconds=timeout_seconds,
            )
            snapshots.append(snapshot)
            if snapshot.get("state") != "closed":
                errors.append(
                    "thinkthread:"
                    f"{pool.get('pool_id')}:cleanup_incomplete:{snapshot.get('state')}"
                )
        except Exception as exc:  # noqa: BLE001 - cleanup must record host failures
            errors.append(
                f"thinkthread:{pool.get('pool_id')}:{type(exc).__name__}:{exc}"
            )
    return snapshots, errors


def close_run_internal_agents(
    *, root_dir: Path | str, run_id: str, timeout_seconds: float = 5.0,
) -> list[str]:
    """Host cleanup only; failed resources retain their recoverable handles."""
    errors = _close_run_internal_drainers(root_dir=root_dir, run_id=run_id, timeout_seconds=timeout_seconds)
    try:
        scope = execution_scope_for_run(root_dir=root_dir, run_id=run_id)
        if scope is not None:
            remaining, process_errors = build_scope_process_registry(root_dir).stop(
                scope_id=str(scope["scope_id"]), attachment=ScopeAttachment.from_id(run_id),
                timeout_seconds=timeout_seconds, kind="internal",
            )
            errors.extend(process_errors)
            if remaining:
                errors.append(f"internal:processes_remaining:{remaining}")
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
        from goal_plus.adaptive_search.scheduler import close_scheduler

        close_scheduler(
            root_dir=root_dir,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must record host failures
        errors.append(f"scheduler:{type(exc).__name__}:{exc}")
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

    def close_pools(
        self, *, attachment: ScopeAttachment, scope_id: str, timeout_seconds: float,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        return _close_attachment_pools(
            root_dir=self.root_dir, attachment=attachment, scope_id=scope_id, timeout_seconds=timeout_seconds,
        )

    def close_drainers(self, *, run_id: str, timeout_seconds: float) -> list[str]:
        return _close_run_internal_drainers(
            root_dir=self.root_dir, run_id=run_id, timeout_seconds=timeout_seconds,
        )

    def codex_run_is_quiescent(self, run_id: str) -> bool:
        return _codex_run_is_durably_quiescent(self.root_dir, run_id)


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


def _codex_run_is_durably_quiescent(root_dir: Path | str, run_id: str) -> bool:
    from goal_plus.candidate_host import candidate_host_queries

    queries = candidate_host_queries(root_dir)
    sessions = queries.sessions(run_id)
    if not sessions:
        return True
    terminal = {"completed", "failed", "interrupted", "timed_out", "cancelled"}
    for session in sessions:
        if session.host != "codex":
            continue
        metadata = session.host_handle.metadata
        if metadata.get("subagent_stop_observed_at"):
            continue
        if str(metadata.get("status") or metadata.get("terminal_status") or "").lower() in terminal:
            continue
        recovery = queries.recovery(run_id, session.candidate_id)
        if (
            recovery is not None and recovery.state == "restored"
            and session.execution_generation < recovery.generation
            and session.agent_session_id in recovery.fenced_session_ids
        ):
            # Restore already required trusted quiescence for these exact sessions.
            continue
        return False
    return True


def execution_scope_observation(*, root_dir: Path | str, scope_id: str) -> ScopeGuardianObservation:
    scope = execution_scope_snapshot(root_dir=root_dir, scope_id=scope_id)
    return ScopeGuardianObservation.model_validate({
        name: scope.get(name) for name in ScopeGuardianObservation.model_fields
    })


def run_guardian(*, root_dir: Path | str, scope_id: str) -> int:
    scope_id = validate_scope_id(scope_id)
    root_dir = Path(root_dir).expanduser().resolve()
    return ScopeGuardian(
        read=lambda: execution_scope_observation(root_dir=root_dir, scope_id=scope_id),
        close=lambda reason: close_execution_scope(root_dir=root_dir, scope_id=scope_id, reason=reason),
        owner_alive=process_alive, pause=time.sleep,
        observe_resources=lambda: observe_execution_processes(root_dir=root_dir, scope_id=scope_id),
    ).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal Plus host execution-scope guardian")
    subparsers = parser.add_subparsers(dest="command", required=True)
    guard = subparsers.add_parser("guard")
    guard.add_argument("--root", required=True)
    guard.add_argument("--scope-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "guard":
        return run_guardian(root_dir=args.root, scope_id=args.scope_id)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
