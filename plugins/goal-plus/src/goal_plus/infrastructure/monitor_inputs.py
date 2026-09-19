from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from goal_plus.bootstrap import build_goal_reconciliation
from goal_plus.projections.publication import publication_summary
from goal_plus.projections.monitor import (
    project_artifact_availability,
    project_evidence_reads,
    project_goal,
    metric_context,
    numeric_value,
    observability_cost,
    parse_utc_timestamp,
    aggregate_search_tasks,
    snapshot_id_from_mutation,
    state_counts,
    project_strategy,
)
from goal_plus.ports.monitor import MonitorCandidateInputs, MonitorSessionInputs
from goal_plus.projections.monitor_workers import project_candidate, project_session
from goal_plus._version import __version__
from goal_plus.domain.agents import (
    INNER_AGENT_AUTORESEARCH,
    INNER_AGENT_COMMON,
    WORKSPACE_LEDGER_PROJECTION_NONE,
    WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV,
)
from goal_plus.infrastructure.state.verifier_views import load_search_verifier_views
from goal_plus.agent_hosts import collect_host_observability
from goal_plus.infrastructure.host_bindings import resolve_inner_agent_binding
from goal_plus.feature_plugins import collect_goal_monitor_features
from goal_plus.infrastructure.state.goal import FileGoalQueries
from goal_plus.host_observability import collect_codex_transcript_observability
from goal_plus.domain.search import (
    AgentSessionRecord,
    FrozenSpec,
    SearchPlan,
)
from goal_plus.ports.state import (
    CandidateRecord,
    RunRecord,
)
from goal_plus.domain.annotation import EvidenceAnnotationTask
from goal_plus.domain.goal import (
    GoalPlusLinkedSearch,
    GoalPlusRecord,
)
from goal_plus.paths import DEFAULT_RUNTIME_ROOT
from goal_plus.domain.results_ledger import RESULTS_TSV_RELATIVE_PATH
from goal_plus.infrastructure.state.files import load_json
from goal_plus.infrastructure.clock import utc_timestamp, utc_timestamp_from_epoch
from goal_plus.shared_dir import SharedDirManager
from goal_plus.projections.statistics import (
    aggregate_usage,
    build_run_statistics,
)


def _path_mtime(path: str | None) -> float | None:
    if not path:
        return None
    candidate = Path(path)
    if not candidate.exists():
        return None
    return candidate.stat().st_mtime


def _path_info(path: str | None) -> dict[str, Any]:
    if not path:
        return {
            "path": None,
            "exists": False,
            "last_updated_at": None,
            "size_bytes": None,
        }
    candidate = Path(path)
    if not candidate.exists():
        return {
            "path": path,
            "exists": False,
            "last_updated_at": None,
            "size_bytes": None,
        }
    stat = candidate.stat()
    return {
        "path": path,
        "exists": True,
        "last_updated_at": utc_timestamp_from_epoch(stat.st_mtime),
        "size_bytes": stat.st_size,
    }


def _evidence_annotation_payload(run_dir: Path) -> dict[str, Any]:
    tasks: list[EvidenceAnnotationTask] = []
    for path in sorted(
        run_dir.glob("candidates/*/evidence-annotations/iteration-*.json")
    ):
        try:
            tasks.append(EvidenceAnnotationTask.model_validate(load_json(path)))
        except (OSError, ValueError):
            continue
    states: dict[str, int] = {}
    for task in tasks:
        states[task.state] = states.get(task.state, 0) + 1

    attempts: list[dict[str, Any]] = []
    for path in sorted((run_dir / "evidence-annotator" / "attempts").glob("*.json")):
        try:
            monitor = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(monitor, dict):
            continue
        attempts.append(
            {
                key: monitor.get(key)
                for key in (
                    "candidate_id",
                    "iteration",
                    "attempt",
                    "agent_harness",
                    "model",
                    "reasoning_effort",
                    "timeout_seconds",
                    "state",
                    "started_at",
                    "updated_at",
                    "elapsed_seconds",
                    "pid",
                    "process_returncode",
                    "stdout_bytes",
                    "stderr_bytes",
                    "json_lines",
                    "non_json_lines",
                    "event_type_counts",
                    "assistant_event_type_counts",
                    "last_events",
                    "detail",
                )
                if monitor.get(key) is not None
            }
            | {"monitor_path": str(path)}
        )
    attempts.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("candidate_id") or ""),
            int(item.get("iteration") or 0),
            int(item.get("attempt") or 0),
        )
    )
    active_states = {"starting", "running", "process_exited"}
    active = [item for item in attempts if item.get("state") in active_states]
    recent = attempts[-8:]
    visible = active + [item for item in recent if item not in active]
    usage_records = []
    for task in tasks:
        if task.attempts == 0:
            continue
        usage = dict(task.usage)
        components = [
            numeric_value(usage.get(field))
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "output_tokens",
            )
        ]
        observed = [value for value in components if value is not None]
        usage["processed_tokens"] = sum(observed) if observed else None
        usage_records.append(usage)
    return {
        "tasks": len(tasks),
        "attempts": sum(task.attempts for task in tasks),
        "views_published": sum(task.view is not None for task in tasks),
        "states": dict(sorted(states.items())),
        "active_attempts": active,
        "recent_attempts": visible,
        "monitor_files": len(attempts),
        "usage": aggregate_usage(usage_records, scope="evidence_annotator_tasks"),
    }


def _goal_metric_contexts(
    root_dir: Path,
    record: GoalPlusRecord | None,
) -> dict[int, dict[str, Any]]:
    """Recover revision-scoped baseline/target data from the append-only event log."""
    if record is None:
        return {}
    contexts: dict[int, dict[str, Any]] = {}
    current_revision = 1
    events_path = root_dir / "goal-plus" / record.goal_plus_id / "events.jsonl"
    try:
        stream = events_path.open("r", encoding="utf-8")
    except OSError:
        stream = None
    if stream is not None:
        with stream:
            for line in stream:
                try:
                    event = _load_json_line(line)
                except ValueError:
                    continue
                payload = event.get("payload")
                payload = payload if isinstance(payload, dict) else {}
                event_type = event.get("event_type")
                if event_type in {"created", "goal_updated"}:
                    revision = payload.get("goal_revision")
                    if isinstance(revision, int):
                        current_revision = revision
                elif event_type == "spec_draft_saved":
                    contexts[current_revision] = payload
    if record.spec_draft is not None:
        contexts[record.goal_revision] = record.spec_draft.model_dump(mode="json")
    return contexts


def _load_json_line(line: str) -> dict[str, Any]:
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")
    return payload


def _latest_mtime(paths: list[str | None]) -> float | None:
    values = [_path_mtime(path) for path in paths]
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _latest_run_id(root_dir: Path) -> str | None:
    run_paths = sorted(
        root_dir.glob("runs/*/run.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not run_paths:
        return None
    return RunRecord.model_validate(load_json(run_paths[0])).run_id


def _run_dir(root_dir: Path, run_id: str) -> Path:
    path = root_dir / "runs" / run_id
    if not path.exists():
        raise FileNotFoundError(f"search run not found: {run_id}")
    return path


def _load_goal_record(root_dir: Path, goal_plus_id: str) -> GoalPlusRecord:
    path = root_dir / "goal-plus" / goal_plus_id / "goal.json"
    if not path.exists():
        raise FileNotFoundError(f"goal-plus record not found: {goal_plus_id}")
    return FileGoalQueries(root_dir).read_goal(goal_plus_id)


def _load_candidates(run_dir: Path) -> list[CandidateRecord]:
    candidate_dir = run_dir / "candidates"
    if not candidate_dir.exists():
        return []
    return [
        CandidateRecord.model_validate(load_json(path))
        for path in sorted(candidate_dir.glob("*/candidate.json"))
    ]


def _load_agent_sessions(run_dir: Path) -> list[AgentSessionRecord]:
    session_dir = run_dir / "agent_sessions"
    if not session_dir.exists():
        return []
    return [
        AgentSessionRecord.model_validate(load_json(path))
        for path in sorted(session_dir.glob("*.json"))
    ]


def _time_advisory_evidence(
    root: Path,
    session: AgentSessionRecord,
) -> dict[str, Any] | None:
    metadata = session.session_handle.metadata
    pi_advisory = metadata.get("time_advisory")
    if isinstance(pi_advisory, dict):
        return pi_advisory
    if session.agent_harness != "codex":
        return None
    path = (
        root
        / "host-logs"
        / "codex-time-advisory"
        / "sent"
        / f"{session.agent_session_id}.json"
    )
    try:
        payload = load_json(path)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_plans(run_dir: Path) -> list[SearchPlan]:
    return [
        SearchPlan.model_validate(load_json(path))
        for path in sorted((run_dir / "plans").glob("plan_*.json"))
    ]


def _agent_observability(
    session: AgentSessionRecord,
    cache: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    key = (session.run_id, session.agent_session_id)
    if cache is not None and key in cache:
        return cache[key]
    try:
        payload = collect_host_observability(session)
    except Exception as exc:
        payload = {
            "schema_version": 3,
            "agent_session_id": session.agent_session_id,
            "run_id": session.run_id,
            "candidate_id": session.candidate_id,
            "agent_harness": session.agent_harness,
            "runtime_provider": session.runtime_provider,
            "source": "collection_failed",
            "identity": {
                "native_session_id": None,
                "external_id": session.session_handle.external_id,
                "nickname": session.session_handle.nickname,
            },
            "execution": {
                "duration_seconds": None,
                "terminal_state": "unknown",
                "timed_out": bool(session.session_handle.metadata.get("timed_out")),
                "runner_failed": bool(
                    session.session_handle.metadata.get("runner_failed")
                ),
            },
            "usage": {"cost_usd": None},
            "context": {
                "tokens": None,
                "context_window": None,
                "percent": None,
                "source": "unknown",
            },
            "artifacts": {
                "event_log": session.session_handle.metadata.get("event_log"),
                "text_log": session.session_handle.metadata.get("text_log"),
                "session_file": session.session_handle.metadata.get("session_file"),
            },
            "handoff": {"present": False, "source_path": None, "error": None},
            "runtime_start": {
                "source": "unavailable",
                "agent": None,
                "agent_harness": session.agent_harness,
            "runtime_provider": session.runtime_provider,
                "mode": None,
                "project_trusted": None,
                "active_tools": [],
                "active_builtin_tools": [],
                "active_non_builtin_tools": [],
                "pi_sandbox": None,
                "pi_builtin_tool_approval": None,
                "filesystem_access": None,
                "network_access": None,
                "cwd": None,
                "captured_at": None,
                "error": f"{type(exc).__name__}: {exc}",
            },
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    if cache is not None:
        cache[key] = payload
    return payload


def _host_cleanup_payload(
    root_dir: Path,
    run: RunRecord,
    frozen: FrozenSpec,
    candidates: list[CandidateRecord],
) -> dict[str, Any]:
    """Project host-local cleanup without exposing its filesystem layout."""

    if (
        frozen.spec.workspace.provider != "thinkthread"
        and frozen.runtime_provider != "thinkthread"
    ):
        return {
            "schema_version": 1,
            "provider": None,
            "state": "not_applicable",
            "candidate_sessions": None,
            "annotation": None,
            "snapshots": None,
            "final_artifacts": None,
            "blockers": [],
        }

    session_states: list[str] = []
    for path in sorted((root_dir / "host-scopes").glob("*/thinkthread-sessions/*.json")):
        resource = load_json(path)
        if resource.get("run_id") == run.run_id:
            state = str(resource.get("cleanup_state") or "open")
            session_states.append(state)
    sessions_complete = (
        all(state == "closed" for state in session_states)
        if session_states
        else not candidates
    )
    candidate_sessions = {
        "observed": len(session_states),
        "states": state_counts(session_states),
        "complete": sessions_complete,
    }

    annotation_applicable = frozen.runtime_provider == "thinkthread"
    annotation_state = "not_applicable"
    annotation_service_state = "not_applicable"
    annotation_observed = False
    if annotation_applicable:
        annotation_root = (
            root_dir / "host-pools" / "thinkthread-annotations" / run.run_id
        )
        state_path = annotation_root / "state.json"
        service_path = annotation_root / "service.json"
        annotation_state = "not_started"
        annotation_service_state = "not_observed"
        if state_path.is_file():
            annotation_observed = True
            try:
                state = load_json(state_path)
                annotation_state = str(state.get("status") or "unknown")
            except (OSError, ValueError, json.JSONDecodeError):
                annotation_state = "unknown"
        if service_path.is_file():
            try:
                service = load_json(service_path)
                annotation_service_state = str(service.get("status") or "unknown")
            except (OSError, ValueError, json.JSONDecodeError):
                annotation_service_state = "unknown"
    annotation_service_complete = annotation_service_state in {
        "not_applicable",
        "not_observed",
        "stopped",
        "exited",
        "stale",
    }
    annotation_complete = not annotation_applicable or (
        (not annotation_observed or annotation_state == "closed")
        and annotation_service_complete
    )
    annotation = {
        "applicable": annotation_applicable,
        "observed": annotation_observed,
        "state": annotation_state,
        "service_state": annotation_service_state,
        "service_complete": annotation_service_complete,
        "complete": annotation_complete,
    }

    expected_snapshot_ids: set[str] = set()
    for mutation in run.provider_mutations:
        snapshot_id = snapshot_id_from_mutation(mutation)
        if snapshot_id is not None:
            expected_snapshot_ids.add(snapshot_id)
    for candidate in candidates:
        for mutation in candidate.provider_mutations:
            snapshot_id = snapshot_id_from_mutation(mutation)
            if snapshot_id is not None:
                expected_snapshot_ids.add(snapshot_id)
    cleanup_by_snapshot = {
        item.resource_id: item
        for item in run.provider_cleanup
        if item.resource_id in expected_snapshot_ids
    }
    snapshot_states = [item.state for item in cleanup_by_snapshot.values()]
    missing_snapshot_records = len(expected_snapshot_ids - cleanup_by_snapshot.keys())
    if not expected_snapshot_ids:
        snapshot_state = "not_applicable"
        snapshots_complete = True
    elif any(state == "failed" for state in snapshot_states):
        snapshot_state = "failed"
        snapshots_complete = False
    elif any(state == "needs_recovery" for state in snapshot_states):
        snapshot_state = "needs_recovery"
        snapshots_complete = False
    elif any(state == "retained" for state in snapshot_states):
        snapshot_state = "retained"
        snapshots_complete = False
    elif missing_snapshot_records:
        snapshot_state = "pending"
        snapshots_complete = False
    elif all(state in {"removed", "already_absent"} for state in snapshot_states):
        snapshot_state = "complete"
        snapshots_complete = True
    else:
        snapshot_state = "pending"
        snapshots_complete = False
    snapshots = {
        "owned_total": len(expected_snapshot_ids),
        "cleanup_records": len(cleanup_by_snapshot),
        "missing_records": missing_snapshot_records,
        "states": state_counts(snapshot_states),
        "state": snapshot_state,
        "complete": snapshots_complete,
    }

    report_path = root_dir / "runs" / run.run_id / "report.md"
    html_path = report_path.with_suffix(".html")
    final_artifacts = {
        "report_markdown": report_path.is_file(),
        "report_html": html_path.is_file(),
        "complete": report_path.is_file() and html_path.is_file(),
    }

    blockers: list[str] = []
    if not sessions_complete:
        blockers.append("candidate_sessions:not_closed")
    if not annotation_complete:
        if not annotation_service_complete:
            blockers.append(f"annotation_service:{annotation_service_state}")
        else:
            blockers.append(f"annotation:{annotation_state}")
    if not final_artifacts["complete"]:
        blockers.append("final_artifacts:not_durable")
    if not snapshots_complete:
        blockers.append(f"snapshots:{snapshot_state}")

    component_states = [
        *session_states,
        annotation_state,
        annotation_service_state,
        snapshot_state,
    ]
    if "failed" in component_states:
        overall_state = "failed"
    elif "needs_recovery" in component_states:
        overall_state = "needs_recovery"
    elif snapshot_state == "retained":
        overall_state = "retained"
    elif not blockers:
        overall_state = "complete"
    else:
        overall_state = "pending"
    return {
        "schema_version": 1,
        "provider": "thinkthread",
        "state": overall_state,
        "candidate_sessions": candidate_sessions,
        "annotation": annotation,
        "snapshots": snapshots,
        "final_artifacts": final_artifacts,
        "blockers": blockers,
    }


def _search_task_payload(
    root_dir: Path,
    task: GoalPlusLinkedSearch,
    *,
    current_run_id: str | None,
    goal_metric_contexts: dict[int, dict[str, Any]],
    now_epoch: float,
    observability_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = task.model_dump(mode="json")
    run_id = task.run_id
    payload.update(
        {
            "is_current": run_id is not None and run_id == current_run_id,
            "run_exists": False,
            "state": None,
            "search_result": "unknown",
            "publication_result": "not_applicable",
            "publication_outcome": None,
            "provider_cleanup": [],
            "artifact_availability": [],
            "host_cleanup": None,
            "run_frozen_spec_id": None,
            "frozen_spec_exists": False,
            "strategy": None,
            "planning_rounds_total": 0,
            "started_rounds_total": 0,
            "candidates_total": 0,
            "candidates_evaluated": 0,
            "worker_sessions_total": 0,
            "verifier_runs_total": 0,
            "evidence_annotations": None,
            "evidence_reads": None,
            "estimated_cost_total": 0.0,
            "statistics": None,
        }
    )
    if run_id is None:
        return payload
    run_dir = root_dir / "runs" / run_id
    run_path = run_dir / "run.json"
    if not run_path.exists():
        return payload

    run = RunRecord.model_validate(load_json(run_path))
    frozen_path = root_dir / "specs" / run.frozen_spec_id / "frozen_spec.json"
    frozen = (
        FrozenSpec.model_validate(load_json(frozen_path))
        if frozen_path.exists()
        else None
    )
    plans = _load_plans(run_dir)
    candidates = _load_candidates(run_dir)
    sessions = _load_agent_sessions(run_dir)
    observations = {
        session.agent_session_id: _agent_observability(session, observability_cache)
        for session in sessions
    }
    estimated_cost_total = sum(
        observability_cost(observation) for observation in observations.values()
    )
    baseline_score = None
    target_score = None
    statistics = None
    if frozen is not None:
        baseline_score, target_score = metric_context(
            frozen,
            task,
            goal_metric_contexts,
        )
        statistics = build_run_statistics(
            run,
            frozen,
            candidates,
            sessions,
            observations,
            baseline_score=baseline_score,
            target_score=target_score,
            now_epoch=now_epoch,
        )
    payload.update(
        {
            "run_exists": True,
            "state": run.state,
            **publication_summary(run),
            "provider_cleanup": [
                item.model_dump(mode="json") for item in run.provider_cleanup
            ],
            "artifact_availability": project_artifact_availability(run),
            "host_cleanup": (
                _host_cleanup_payload(root_dir, run, frozen, candidates)
                if frozen is not None
                else None
            ),
            "run_frozen_spec_id": run.frozen_spec_id,
            "frozen_spec_exists": frozen is not None,
            "strategy": (
                {
                    "name": frozen.spec.strategy.name,
                    "agent_harness": frozen.agent_harness,
                    "runtime_provider": frozen.runtime_provider,
                }
                if frozen is not None
                else None
            ),
            "planning_rounds_total": len(plans),
            "started_rounds_total": sum(
                1 for plan in plans if plan.status == "started"
            ),
            "candidates_total": len(candidates),
            "candidates_evaluated": sum(
                1 for candidate in candidates if candidate.status == "evaluated"
            ),
            "worker_sessions_total": len(sessions),
            "verifier_runs_total": sum(
                len(candidate.iterations) for candidate in candidates
            ),
            "evidence_annotations": _evidence_annotation_payload(run_dir),
            "evidence_reads": project_evidence_reads(sessions),
            "estimated_cost_total": estimated_cost_total,
            "statistics": statistics,
        }
    )
    return payload


def goal_plus_monitor_snapshot(
    root_dir: Path | str = DEFAULT_RUNTIME_ROOT,
    *,
    goal_plus_id: str | None = None,
    run_id: str | None = None,
    stale_after_seconds: int = 600,
    feature_plugins: list[str] | None = None,
) -> dict[str, Any]:
    """Read-only monitoring snapshot for Goal Plus/Search runs.

    This function intentionally does not wait for workers, inspect live
    processes, or mutate runtime state. It summarizes durable `.gp` and
    host-handle evidence so a monitoring agent can poll cheaply.
    """

    root = Path(root_dir).resolve()
    now = time.time()
    warnings: list[dict[str, Any]] = []
    goal_record: GoalPlusRecord | None = None
    if goal_plus_id:
        goal_record = _load_goal_record(root, goal_plus_id)
        linked = goal_record.linked_search
        if run_id is None and linked and linked.run_id:
            run_id = linked.run_id

    if run_id is None and goal_plus_id is None:
        run_id = _latest_run_id(root)
        if run_id:
            warnings.append({"kind": "inferred_latest_run", "run_id": run_id})
    elif run_id is None and goal_record is not None:
        warnings.append(
            {
                "kind": "goal_without_linked_search",
                "goal_plus_id": goal_record.goal_plus_id,
            }
        )

    current_search_run_id = (
        goal_record.linked_search.run_id
        if goal_record is not None and goal_record.linked_search is not None
        else None
    )
    task_links = list(goal_record.search_tasks) if goal_record is not None else []
    if goal_record is None and run_id is not None:
        run_record = RunRecord.model_validate(
            load_json(_run_dir(root, run_id) / "run.json")
        )
        task_links = [
            GoalPlusLinkedSearch(
                frozen_spec_id=run_record.frozen_spec_id,
                run_id=run_record.run_id,
                linked_at=run_record.created_at,
            )
        ]
        current_search_run_id = run_id

    linked_run_ids = {task.run_id for task in task_links if task.run_id is not None}
    if goal_record is not None and run_id is not None and run_id not in linked_run_ids:
        warnings.append(
            {
                "kind": "run_not_linked_to_goal",
                "goal_plus_id": goal_record.goal_plus_id,
                "run_id": run_id,
            }
        )

    observability_cache: dict[tuple[str, str], dict[str, Any]] = {}
    goal_metric_contexts = _goal_metric_contexts(root, goal_record)
    search_tasks = [
        _search_task_payload(
            root,
            task,
            current_run_id=current_search_run_id,
            goal_metric_contexts=goal_metric_contexts,
            now_epoch=now,
            observability_cache=observability_cache,
        )
        for task in task_links
    ]
    search_task_aggregate = aggregate_search_tasks(search_tasks)
    terminal_run_states = {"promoted", "aborted", "failed"}
    for task in search_tasks:
        task_run_id = task.get("run_id")
        if not task["run_exists"]:
            warnings.append(
                {
                    "kind": "linked_search_run_missing",
                    "goal_plus_id": goal_record.goal_plus_id if goal_record else None,
                    "run_id": task_run_id,
                }
            )
            continue
        if task.get("frozen_spec_id") != task.get("run_frozen_spec_id"):
            warnings.append(
                {
                    "kind": "linked_search_spec_mismatch",
                    "run_id": task_run_id,
                    "linked_frozen_spec_id": task.get("frozen_spec_id"),
                    "run_frozen_spec_id": task.get("run_frozen_spec_id"),
                }
            )
        if not task["frozen_spec_exists"]:
            warnings.append(
                {
                    "kind": "linked_search_frozen_spec_missing",
                    "run_id": task_run_id,
                    "frozen_spec_id": task.get("run_frozen_spec_id"),
                }
            )
        if not task["is_current"] and task["state"] not in terminal_run_states:
            warnings.append(
                {
                    "kind": "superseded_search_task_not_terminal",
                    "run_id": task_run_id,
                    "state": task["state"],
                }
            )
    current_task = next((task for task in search_tasks if task["is_current"]), None)
    if (
        goal_record is not None
        and goal_record.status == "complete"
        and current_task is not None
        and current_task["state"] != "promoted"
    ):
        warnings.append(
            {
                "kind": "completed_goal_current_search_not_promoted",
                "goal_plus_id": goal_record.goal_plus_id,
                "run_id": current_task.get("run_id"),
                "state": current_task.get("state"),
            }
        )

    run_payload: dict[str, Any] | None = None
    strategy_payload: dict[str, Any] | None = None
    candidates_payload: dict[str, dict[str, Any]] = {}
    subagents: list[dict[str, Any]] = []
    selected_run_statistics: dict[str, Any] | None = None
    main_agent = {
        "elapsed_seconds": None,
        "run_age_seconds": None,
        "observed_duration_seconds": None,
        "estimated_cost_total": 0.0,
        "subagent_count": 0,
        "worker_dispatch_count": 0,
        "verifier_count": 0,
        "context_tokens_max": None,
        "context_percent_max": None,
    }

    selected_run_exists = (
        run_id is not None and (root / "runs" / run_id / "run.json").exists()
    )
    if run_id is not None and selected_run_exists:
        run_path = _run_dir(root, run_id)
        run = RunRecord.model_validate(load_json(run_path / "run.json"))
        frozen = FrozenSpec.model_validate(
            load_json(root / "specs" / run.frozen_spec_id / "frozen_spec.json")
        )
        verifier_enabled = frozen.spec.strategy.requires_llm_verifier
        candidates = _load_candidates(run_path)
        sessions = _load_agent_sessions(run_path)
        plans = _load_plans(run_path)
        plans_count = len(plans)
        started_rounds_total = sum(1 for plan in plans if plan.status == "started")
        latest_plan = plans[-1] if plans else None
        strategy_payload = project_strategy(frozen, latest_plan)
        by_candidate = {candidate.candidate_id: candidate for candidate in candidates}
        sessions_by_candidate: dict[str, list[AgentSessionRecord]] = {}
        for session in sessions:
            sessions_by_candidate.setdefault(session.candidate_id, []).append(session)

        created_at_epoch = parse_utc_timestamp(run.created_at)
        if created_at_epoch is not None:
            main_agent["run_age_seconds"] = max(0.0, now - created_at_epoch)

        observations = {
            session.agent_session_id: _agent_observability(session, observability_cache)
            for session in sessions
        }
        selected_task_link = next(
            (task for task in task_links if task.run_id == run.run_id),
            None,
        )
        baseline_score, target_score = metric_context(
            frozen,
            selected_task_link,
            goal_metric_contexts,
        )
        selected_run_statistics = build_run_statistics(
            run,
            frozen,
            candidates,
            sessions,
            observations,
            baseline_score=baseline_score,
            target_score=target_score,
            now_epoch=now,
        )
        observed_duration = selected_run_statistics["run"]["observed_duration_seconds"]
        main_agent["elapsed_seconds"] = observed_duration
        main_agent["observed_duration_seconds"] = observed_duration

        run_payload = {
            "run_id": run.run_id,
            "state": run.state,
            **publication_summary(run),
            "provider_cleanup": [
                item.model_dump(mode="json") for item in run.provider_cleanup
            ],
            "artifact_availability": project_artifact_availability(run),
            "host_cleanup": _host_cleanup_payload(root, run, frozen, candidates),
            "frozen_spec_id": run.frozen_spec_id,
            "created_at": run.created_at,
            "source_path": run.source_path,
            "source_artifact_ref": (
                run.source_artifact_ref.model_dump(mode="json")
                if run.source_artifact_ref is not None
                else None
            ),
            "origin_verification": (
                frozen.origin_verification.model_dump(mode="json")
                if frozen.origin_verification is not None
                else None
            ),
            "workspace_bootstrap_verification": (
                run.workspace_bootstrap_verification.model_dump(mode="json")
                if run.workspace_bootstrap_verification is not None
                else None
            ),
            "plans_count": plans_count,
            "planning_rounds_total": plans_count,
            "started_rounds_total": started_rounds_total,
            "candidates_total": len(candidates),
            "candidates_evaluated": sum(
                1 for candidate in candidates if candidate.status == "evaluated"
            ),
            "best_candidate_id": run.best_candidate_id,
            "best_score": run.best_score,
            "selected_candidate_id": run.selected_candidate_id,
            "selected_score": run.selected_score,
            "selected_hard_score": run.selected_hard_score,
            "selected_quality_score": run.selected_quality_score,
            "selection_trace": run.selection_trace,
            "selection_exclusions": run.selection_exclusions,
            "selected_iteration": run.selected_iteration,
            "selected_git_head": run.selected_git_head,
            "promotion": (
                run.promotion.model_dump(mode="json")
                if run.promotion is not None
                else None
            ),
            "budget_used": run.budget_used,
            "evidence_annotations": _evidence_annotation_payload(run_path),
            "evidence_reads": project_evidence_reads(sessions),
        }
        if frozen.spec.shared_dir.enabled:
            families = SharedDirManager(run_path).load_families()
            tools = {
                tool.tool_id: tool
                for candidate in candidates
                for iteration in candidate.iterations
                for tool in iteration.shared_tools
            }
            run_payload["shared_tool_families"] = {
                "family_count": len(families),
                "pending_count": sum(
                    family.pending_head is not None for family in families
                ),
                "discoverable_count": sum(
                    family.discoverable_head is not None for family in families
                ),
                "families": [
                    {
                        **family.model_dump(mode="json"),
                        "pending_version": (
                            tools[family.pending_head].version
                            if family.pending_head in tools
                            else None
                        ),
                        "discoverable_version": (
                            tools[family.discoverable_head].version
                            if family.discoverable_head in tools
                            else None
                        ),
                    }
                    for family in families
                ],
            }

        verifier_tasks_by_candidate: dict[str, list[Any]] = {}
        verifier_records_by_candidate: dict[str, list[Any]] = {}
        verifier_views = (
            load_search_verifier_views(run_path)
            if verifier_enabled
            else None
        )
        if verifier_views is not None:
            for task in verifier_views.verifier_tasks:
                verifier_tasks_by_candidate.setdefault(
                    task.candidate_id, []
                ).append(task)
            for record in verifier_views.verifier_records:
                verifier_records_by_candidate.setdefault(
                    record.subject.candidate_id, []
                ).append(record)
            task_states = state_counts(
                [task.state for task in verifier_views.verifier_tasks]
            )
            run_payload["llm_verifier"] = {
                "tasks_total": len(verifier_views.verifier_tasks),
                "task_states": task_states,
                "records_total": len(verifier_views.verifier_records),
            }
            allocation_decisions = [
                item.model_dump(mode="json")
                for item in verifier_views.allocation_decisions
            ]
            expansion_counts = [
                len(item.get("expansions") or []) for item in allocation_decisions
            ]
            run_payload["allocation_decisions"] = {
                "total": len(allocation_decisions),
                "pending": sum(
                    item.get("status") == "pending" for item in allocation_decisions
                ),
                "interrupted": sum(
                    item.get("status") == "interrupted" for item in allocation_decisions
                ),
                "applied": sum(
                    item.get("status") == "applied" for item in allocation_decisions
                ),
                "superseded": sum(
                    item.get("status") == "superseded" for item in allocation_decisions
                ),
                "terminal_error": sum(
                    item.get("status") == "terminal_error" for item in allocation_decisions
                ),
                "expansions": sum(expansion_counts),
                "max_atomic_expansions": max(expansion_counts, default=0),
                "items": allocation_decisions,
            }

        for candidate in candidates:
            candidate_sessions = sessions_by_candidate.get(candidate.candidate_id, [])
            results_path = candidate.task.workspace / RESULTS_TSV_RELATIVE_PATH
            worker_policy = candidate.task.strategy_metadata.get("worker_policy", {})
            persisted_binding = worker_policy.get("inner_agent_binding")
            workspace_ledger_projection = (
                persisted_binding.get("workspace_ledger_projection")
                if isinstance(persisted_binding, dict)
                else None
            )
            if workspace_ledger_projection not in {
                WORKSPACE_LEDGER_PROJECTION_NONE,
                WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV,
            }:
                if results_path.is_file():
                    workspace_ledger_projection = (
                        WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
                    )
                else:
                    inner_agent = str(
                        worker_policy.get("inner_agent")
                        or frozen.spec.strategy.inner_agent
                        or (
                            INNER_AGENT_COMMON
                            if frozen.runtime_provider == "thinkthread"
                            else INNER_AGENT_AUTORESEARCH
                        )
                    )
                    workspace_ledger_projection = resolve_inner_agent_binding(
                        frozen.agent_harness,
                        inner_agent,
                        frozen.runtime_provider,
                    ).workspace_ledger_projection
            if (
                candidate.task.workspace_provider == "thinkthread"
                and workspace_ledger_projection
                == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
            ):
                results_tsv = {
                    "path": RESULTS_TSV_RELATIVE_PATH,
                    "exists": True if candidate.inner_agent_state else None,
                    "last_updated_at": None,
                    "size_bytes": None,
                    "scope": "thinkthread_child_private",
                }
            else:
                results_tsv = _path_info(
                    str(results_path)
                    if workspace_ledger_projection
                    == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
                    else None
                )
            results_tsv["row_count"] = (
                len(candidate.results_ledger)
                if workspace_ledger_projection
                == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
                else None
            )
            candidates_payload[candidate.candidate_id] = project_candidate(
                MonitorCandidateInputs(
                    candidate,
                    candidate_sessions,
                    frozen.spec.metric_direction,
                    verifier_enabled,
                    verifier_records_by_candidate.get(candidate.candidate_id, []),
                    verifier_tasks_by_candidate.get(candidate.candidate_id, []),
                    workspace_ledger_projection,
                    results_tsv,
                )
            )
            if not candidate_sessions and candidate.status == "created":
                warnings.append(
                    {
                        "kind": "candidate_without_agent_session",
                        "candidate_id": candidate.candidate_id,
                    }
                )

        for session in sessions:
            observation = observations[session.agent_session_id]
            artifacts = observation.get("artifacts")
            artifacts = artifacts if isinstance(artifacts, dict) else {}
            paths = {
                key: value if isinstance(value := artifacts.get(key), str) else None
                for key in ("event_log", "text_log", "session_file")
            }
            facts = MonitorSessionInputs(
                session,
                by_candidate.get(session.candidate_id),
                observation,
                _time_advisory_evidence(root, session),
                {key: _path_info(value) for key, value in paths.items()},
                _latest_mtime(list(paths.values())),
                now,
                stale_after_seconds,
            )
            payload, metrics, session_warnings = project_session(facts)
            subagents.append(payload)
            warnings.extend(session_warnings)
            main_agent["estimated_cost_total"] += metrics["estimated_cost_total"]
            for key in ("context_tokens_max", "context_percent_max"):
                if metrics[key] is not None:
                    main_agent[key] = max(main_agent[key] or 0, metrics[key])

        main_agent["subagent_count"] = len(sessions)
        main_agent["worker_dispatch_count"] = sum(
            int(session.session_handle.metadata.get("dispatch_count") or 1)
            for session in sessions
        )
        main_agent["verifier_count"] = sum(
            len(candidate.iterations) for candidate in candidates
        )

    orchestrator_observability: dict[str, Any] | None = None
    if goal_record is not None:
        persisted_path = (
            root
            / "goal-plus"
            / goal_record.goal_plus_id
            / "orchestrator-observability.json"
        )
        try:
            persisted = load_json(persisted_path)
            if isinstance(persisted, dict):
                orchestrator_observability = persisted
        except FileNotFoundError:
            pass
        except (OSError, ValueError, json.JSONDecodeError):
            warnings.append(
                {
                    "kind": "orchestrator_observability_invalid",
                    "path": str(persisted_path),
                }
            )
    if (
        orchestrator_observability is None
        and goal_record is not None
        and goal_record.active_session is not None
        and goal_record.active_session.agent_harness == "codex"
        and goal_record.active_session.transcript_path
    ):
        transcript_path = Path(goal_record.active_session.transcript_path).expanduser()
        if transcript_path.is_file():
            orchestrator_observability = collect_codex_transcript_observability(
                transcript_path,
                since=goal_record.created_at,
            )
        else:
            warnings.append(
                {
                    "kind": "orchestrator_transcript_missing",
                    "path": str(transcript_path),
                }
            )

    aggregate_statistics = search_task_aggregate.get("statistics") or {}
    worker_usage = aggregate_statistics.get("usage")
    annotator_usage = search_task_aggregate.get("annotator_usage") or {}
    usage_sources: list[dict[str, Any] | None] = []
    if (
        isinstance(worker_usage, dict)
        and int(worker_usage.get("sources_total") or 0) > 0
    ):
        usage_sources.append(worker_usage)
    if (
        isinstance(annotator_usage, dict)
        and int(annotator_usage.get("sources_total") or 0) > 0
    ):
        usage_sources.append(annotator_usage)
    if orchestrator_observability is not None:
        usage_sources.append(orchestrator_observability.get("usage"))
    total_usage = aggregate_usage(usage_sources, scope="goal_plus_total")
    total_usage["complete"] = bool(
        total_usage["complete"]
        and all(
            source.get("complete") is not False for source in usage_sources if source
        )
        and (goal_record is None or orchestrator_observability is not None)
    )
    unavailable_metrics = [
        "orchestrator_cost_usd",
        "hardware_utilization",
        "semantic_candidate_coverage",
        "redundant_attempt_rate",
        "temporal_collision_rate",
        "research_rollup_quality",
        "normalized_score",
        "orchestrator_usage_breakdown",
        "promotion_attempt_history",
    ]
    if orchestrator_observability is None:
        unavailable_metrics.append("orchestrator_token_usage")

    goal_events = (
        FileGoalQueries(root).read_events(goal_record.goal_plus_id)
        if goal_record is not None
        else []
    )
    feature_payloads = collect_goal_monitor_features(
        root,
        goal_record,
        goal_events,
        feature_plugins,
    )

    execution_scope: dict[str, Any] | None = None
    if goal_record is not None or run_id is not None:
        try:
            raw_scope = _monitor_scope(root, goal_record, run_id)
            if raw_scope is not None:
                cleanup = raw_scope.get("cleanup")
                execution_scope = {
                    "scope_id": raw_scope.get("scope_id"),
                    "agent_harness": raw_scope.get("agent_harness"),
                    "owner_session_id": raw_scope.get("owner_session_id"),
                    "state": raw_scope.get("state"),
                    "active_count": (
                        cleanup.get("active_count")
                        if isinstance(cleanup, dict)
                        else None
                    ),
                    "close_reason": raw_scope.get("close_reason"),
                    "cleanup_errors": (
                        cleanup.get("errors") if isinstance(cleanup, dict) else []
                    ),
                    "updated_at": raw_scope.get("updated_at"),
                }
        except (OSError, ValueError, json.JSONDecodeError):
            warnings.append({"kind": "execution_scope_invalid", "run_id": run_id})

    return {
        "ok": True,
        "schema_version": 1,
        "goal_plus_version": __version__,
        "snapshot_at": utc_timestamp(),
        "root_dir": str(root),
        "goal_plus": project_goal(goal_record),
        "reconciliation": build_goal_reconciliation(root).reconciliation(
            goal_record.goal_plus_id
        )
        if goal_record
        else None,
        "feature_plugins": feature_payloads,
        "selected_run_id": run_id,
        "search_tasks": search_tasks,
        "search_task_aggregate": search_task_aggregate,
        "run": run_payload,
        "strategy": strategy_payload,
        "statistics": {
            "schema_version": 1,
            "selected_run": selected_run_statistics,
            "orchestrator": orchestrator_observability,
            "annotator_usage": annotator_usage,
            "total_usage": total_usage,
            "unavailable_metrics": unavailable_metrics,
        },
        "main_agent": main_agent,
        "execution_scope": execution_scope,
        "candidates": candidates_payload,
        "subagents": subagents,
        "warnings": warnings,
    }


def _monitor_scope(root: Path, goal: GoalPlusRecord | None, run_id: str | None):
    from goal_plus.execution.scope import newest_scope, scope_attachments
    from goal_plus.ports.scope import ScopeAttachment
    from goal_plus.bootstrap import build_scope_state

    records = list(build_scope_state(root).records())
    if goal is not None:
        attachment = ScopeAttachment.from_id(goal.goal_plus_id)
        scope = newest_scope(s for s in records if attachment in scope_attachments(s))
        if scope is not None:
            return scope
    if run_id is None:
        return None
    attachment = ScopeAttachment.from_id(run_id)
    return newest_scope(s for s in records if attachment in scope_attachments(s))
