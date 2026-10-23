from __future__ import annotations

import json


from pathlib import Path

from typing import Any

from goal_plus._version import __version__
from goal_plus.installation import snapshot as installation_snapshot

from goal_plus.agent_hosts import collect_host_observability

from goal_plus.infrastructure.state.verifier_views import load_search_verifier_views

from goal_plus.infrastructure.state.goal import FileGoalQueries


from goal_plus.domain.search import (
    AgentSessionRecord,
    FrozenSpec,
)


from goal_plus.domain.annotation import EvidenceAnnotationTask

from goal_plus.domain.goal import GoalPlusRecord

from goal_plus.domain.evidence import IterationRecord

from goal_plus.monitor import goal_plus_monitor_snapshot

from goal_plus.infrastructure.evidence_attachments import FileEvidenceAttachments

from goal_plus.infrastructure.state.files import load_json

from goal_plus.infrastructure.state.search_run import FileSearchRunState
from goal_plus.infrastructure.report_references import collect_reference_impacts
from goal_plus.infrastructure.report_transcripts import attach_transcript_links
from goal_plus.ports.report import ReportTaskInputs
from goal_plus.ports.report import SearchVerifierViews
from goal_plus.projections.report_tasks import task_details
from goal_plus.projections.report_html import REPORT_SCHEMA_VERSION
from goal_plus.projections.report_statistics import (
    build_timeline,
    build_loop_agent_statistics,
    build_stop_hook_statistics,
)


def _find_goal_record(root: Path, run_id: str) -> GoalPlusRecord | None:
    runtime = FileGoalQueries(root)
    for path in sorted((root / "goal-plus").glob("*/goal.json")):
        try:
            record = runtime.read_goal(path.parent.name)
        except (OSError, ValueError):
            continue
        if any(task.run_id == run_id for task in record.search_tasks):
            return record
    return None


def _collect_observability(session: AgentSessionRecord) -> dict[str, Any]:
    try:
        return collect_host_observability(session)
    except Exception as exc:
        return {
            "source": "collection_failed",
            "execution": {
                "terminal_state": "unknown",
                "started_at": session.created_at,
                "ended_at": session.updated_at,
                "duration_seconds": None,
                "timed_out": bool(session.session_handle.metadata.get("timed_out")),
                "runner_failed": bool(
                    session.session_handle.metadata.get("runner_failed")
                ),
            },
            "usage": {},
            "context": {},
            "errors": [f"{type(exc).__name__}: {exc}"],
        }


def _report_iteration_payload(
    run_dir: Path,
    run_id: str,
    candidate_id: str,
    iteration: IterationRecord,
    *,
    frozen: FrozenSpec,
    verifier_views: SearchVerifierViews | None,
) -> dict[str, Any]:
    annotation_path = (
        run_dir
        / "candidates"
        / candidate_id
        / "evidence-annotations"
        / f"iteration-{iteration.iteration:04d}.json"
    )
    annotation = (
        EvidenceAnnotationTask.model_validate(load_json(annotation_path))
        if annotation_path.exists()
        else None
    )
    if annotation is not None and (
        annotation.run_id != run_id
        or annotation.candidate_id != candidate_id
        or annotation.iteration != iteration.iteration
        or annotation.attempt_commit != iteration.git_head
    ):
        raise RuntimeError("evidence annotation does not match iteration")

    view = (
        annotation.view
        if annotation is not None and annotation.state == "completed"
        else None
    )
    if view is not None and (
        view.run_id != run_id
        or view.candidate_id != candidate_id
        or view.iteration != iteration.iteration
        or view.attempt_commit != iteration.git_head
    ):
        raise RuntimeError("evidence view does not match iteration")

    annotation_monitor = None
    if annotation is not None and annotation.attempt_history:
        monitor_relative = annotation.attempt_history[-1].get("monitor_path")
        if isinstance(monitor_relative, str) and monitor_relative:
            runtime_root = run_dir.parents[1].resolve()
            monitor_path = (runtime_root / monitor_relative).resolve()
            if not monitor_path.is_relative_to(runtime_root):
                raise RuntimeError("evidence annotation monitor escapes runtime root")
            if monitor_path.exists():
                monitor = load_json(monitor_path)
                if not isinstance(monitor, dict) or (
                    monitor.get("run_id") != run_id
                    or monitor.get("candidate_id") != candidate_id
                    or monitor.get("iteration") != iteration.iteration
                    or monitor.get("attempt") != annotation.attempts
                ):
                    raise RuntimeError(
                        "evidence annotation monitor does not match iteration"
                    )
                annotation_monitor = monitor

    payload = {
        "candidate_id": candidate_id,
        "iteration": iteration.iteration,
        "frozen_spec_id": frozen.frozen_spec_id,
        "spec_hash": frozen.spec_hash,
        "metric_name": frozen.spec.metric_name,
        "metric_direction": frozen.spec.metric_direction,
        "agent_session_id": iteration.agent_session_id,
        "settlement_id": iteration.settlement_id,
        "selected_model": iteration.selected_model,
        "exact_model_ref": iteration.exact_model_ref,
        "adapter_version": iteration.adapter_version,
        "score": iteration.score,
        "process_passed": iteration.process_passed,
        "hypothesis": iteration.hypothesis,
        "summary": iteration.summary,
        "failure_class": iteration.failure_class,
        "git_head": iteration.git_head,
        "disposition": iteration.disposition,
        "restored_to_iteration": iteration.restored_to_iteration,
        "restored_to_git_head": iteration.restored_to_git_head,
        "workspace_git_head_after_settlement": (
            iteration.workspace_git_head_after_settlement
        ),
        "created_at": iteration.created_at,
        "changed_files": iteration.changed_files,
        "view": view.description if view is not None else None,
        "view_state": annotation.state if annotation is not None else "not_requested",
        "view_created_at": view.created_at if view is not None else None,
        "view_error": annotation.last_error if annotation is not None else None,
        "annotation_attempts": annotation.attempts if annotation is not None else 0,
        "annotation_monitor": annotation_monitor,
        "published_tool_views": (
            [tool_view.model_dump(mode="json") for tool_view in view.tool_views]
            if view is not None
            else []
        ),
        "adopted_tools": [
            adopted_tool.model_dump(mode="json")
            for adopted_tool in iteration.adopted_tools
        ],
        "adoption_confounded": iteration.adoption_confounded,
        "toolization_decision": (
            iteration.toolization_decision.model_dump(mode="json")
            if iteration.toolization_decision is not None
            else None
        ),
        "toolization_advisories": list(iteration.toolization_advisories),
        "shared_tool_staged_entries": list(iteration.shared_tool_staged_entries),
        "shared_tool_publish_status": iteration.shared_tool_publish_status,
    }
    if verifier_views is not None:
        verifier_task = next(
            (
                task
                for task in verifier_views.verifier_tasks
                if task.subject.candidate_id == candidate_id
                and task.subject.iteration == iteration.iteration
                and task.subject.settlement_id == iteration.settlement_id
                and task.subject.settlement.attempt_artifact_hash
                == iteration.artifact_hash
            ),
            None,
        )
        verifier_record = next(
            (
                record
                for record in verifier_views.verifier_records
                if verifier_task is not None and record.task_id == verifier_task.task_id
            ),
            None,
        )
        payload.update(
            {
                "verifier_task": (
                    verifier_task.model_dump(mode="json")
                    if verifier_task is not None
                    else None
                ),
                "verifier_record": (
                    verifier_record.model_dump(mode="json")
                    if verifier_record is not None
                    else None
                ),
            }
        )
    return payload



def _collect_shared_knowledge(
    root: Path, run_id: str, frozen: FrozenSpec, candidates: list[Any]
) -> tuple[Any, dict[str, dict[str, Any]]]:
    """Collect the shared knowledge view and outcome map for the report.

    Returns (SharedKnowledgeView | None, outcome_by_record) — the caller
    renders them together with Evidence entries in unified card format.
    """
    from goal_plus.domain.shared_knowledge import project_shared_knowledge
    from goal_plus.infrastructure.state.shared_knowledge import (
        FileSharedKnowledgeState,
    )

    if not frozen.spec.shared_knowledge.enabled:
        return None, {}
    records = FileSharedKnowledgeState(root).load_knowledge_records(run_id)
    if not records:
        return None, {}
    view = project_shared_knowledge(records, frozen.spec.shared_knowledge)
    settled = {
        f"{candidate.candidate_id}:{iteration.iteration}": {
            "iteration_ref": f"{candidate.candidate_id}:{iteration.iteration}",
            "score": iteration.score,
            "disposition": iteration.disposition,
        }
        for candidate in candidates
        for iteration in candidate.iterations
    }
    outcomes = {
        record.record_id: settled[record.evidence_ref]
        for record in records
        if record.evidence_ref in settled
    }
    return view, outcomes


def _task_details(
    root: Path, task_summary: dict[str, Any], report_run_id: str
) -> dict[str, Any]:
    run_id = task_summary.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {
            **task_summary,
            "is_report_run": False,
            "plans": [],
            "candidates": [],
            "sessions": [],
            "shared_knowledge_view": None,
            "knowledge_outcomes": {},
        }
    state = FileSearchRunState(root)
    run = state.read_run(run_id)
    frozen = state.read_frozen_spec(run.frozen_spec_id)
    candidates = state.read_candidates(run_id)
    spec_ids = {run.frozen_spec_id}
    spec_ids.update(c.active_frozen_spec_id or run.frozen_spec_id for c in candidates)
    spec_ids.update(i.frozen_spec_id or run.frozen_spec_id for c in candidates for i in c.iterations)
    spec_ids.update(revision.frozen_spec_id for c in candidates for revision in c.spec_revisions)
    spec_ids.update(revision.parent_frozen_spec_id for c in candidates for revision in c.spec_revisions)
    if run.selected_frozen_spec_id is not None:
        spec_ids.add(run.selected_frozen_spec_id)
    specs: dict[str, FrozenSpec] = {}
    while spec_ids:
        spec_id = spec_ids.pop()
        if spec_id in specs:
            continue
        related = state.read_frozen_spec(spec_id)
        specs[spec_id] = related
        if related.parent_frozen_spec_id is not None:
            spec_ids.add(related.parent_frozen_spec_id)
    sessions = state.read_agent_sessions(run_id)
    shared_knowledge_view, knowledge_outcomes = _collect_shared_knowledge(
        root, run_id, frozen, candidates
    )
    run_dir = state.run_dir(run_id)
    verifier_enabled = frozen.spec.strategy.requires_llm_verifier
    verifier_views = load_search_verifier_views(run_dir) if verifier_enabled else None
    inputs = ReportTaskInputs(
        run=run,
        frozen=frozen,
        candidate_specs={c.candidate_id: specs[c.active_frozen_spec_id or run.frozen_spec_id] for c in candidates},
        frozen_specs=specs,
        candidates=candidates,
        sessions=sessions,
        plans=state.read_plans(run_id),
        observations={s.agent_session_id: _collect_observability(s) for s in sessions},
        verifier_views=verifier_views,
        iterations={
            (c.candidate_id, i.iteration): _report_iteration_payload(
                run_dir,
                run_id,
                c.candidate_id,
                i,
                frozen=specs[i.frozen_spec_id or run.frozen_spec_id],
                verifier_views=verifier_views,
            )
            for c in candidates
            for i in c.iterations
        },
        reference_impacts=collect_reference_impacts(
            root,
            run,
            frozen,
            candidates,
        ),
        shared_knowledge_view=shared_knowledge_view,
        knowledge_outcomes=knowledge_outcomes,
    )
    result = task_details(inputs, task_summary, report_run_id)
    FileEvidenceAttachments(root).attach_external_evaluations(
        run_id, [i for c in result["candidates"] for i in c["iterations"]]
    )
    attach_transcript_links(root, result)
    return result


def _build_stop_hook_statistics(
    root: Path, *, goal_plus_id: str | None, run_ids: set[str]
) -> dict[str, Any]:
    event_dir = root / "host-logs" / "codex-hook-events"
    events = []
    for path in sorted(event_dir.glob("*.json")):
        try:
            events.append(load_json(path))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return build_stop_hook_statistics(
        events,
        source_available=event_dir.is_dir(),
        source_path=str(event_dir),
        goal_plus_id=goal_plus_id,
        run_ids=run_ids,
    )


def build_html_report_data(root_dir: Path | str, run_id: str) -> dict[str, Any]:
    root = Path(root_dir).resolve()
    goal = _find_goal_record(root, run_id)
    goal_id = goal.goal_plus_id if goal is not None else None
    snapshot = goal_plus_monitor_snapshot(
        root,
        goal_plus_id=goal_id,
        run_id=run_id,
    )
    task_summaries = snapshot.get("search_tasks")
    task_summaries = task_summaries if isinstance(task_summaries, list) else []
    tasks = [
        _task_details(root, summary, run_id)
        for summary in task_summaries
        if isinstance(summary, dict) and summary.get("run_exists")
    ]
    goal_runtime = FileGoalQueries(root)
    goal_events = goal_runtime.read_events(goal_id) if goal_id is not None else []
    timeline = build_timeline(goal, goal_events, tasks)
    stop_hook_statistics = _build_stop_hook_statistics(
        root,
        goal_plus_id=goal_id,
        run_ids={
            str(task["run_id"])
            for task in tasks
            if isinstance(task.get("run_id"), str) and task.get("run_id")
        }
        | {run_id},
    )
    loop_agent_statistics = build_loop_agent_statistics(
        tasks,
        stop_hook_statistics,
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "goal_plus_version": __version__,
        "goal_plus_build": installation_snapshot().get("build"),
        "generated_at": snapshot.get("snapshot_at"),
        "goal_plus_id": goal_id,
        "report_run_id": run_id,
        "snapshot": snapshot,
        "search_tasks": tasks,
        "timeline": timeline,
        "stop_hook_statistics": stop_hook_statistics,
        "loop_agent_statistics": loop_agent_statistics,
        "agent_transcript": _agent_transcript_export(root),
    }


def _agent_transcript_export(root: Path) -> dict[str, str] | None:
    """Optional pointer to a local debug transcript export beside run reports."""
    index = root / "host-logs" / "agent-transcript" / "index.html"
    if index.is_file():
        return {"href": "../../host-logs/agent-transcript/index.html"}
    return None
