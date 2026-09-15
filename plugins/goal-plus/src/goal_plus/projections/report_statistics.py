from __future__ import annotations


from collections import Counter


from math import isfinite


from typing import Any

from goal_plus.domain.goal import GoalPlusRecord
from goal_plus.projections.usage import normalize_pi_cost
from goal_plus.projections.report_format import (
    parse_epoch,
    finite_float,
    is_better_score,
    per_minute,
    collect_session_scores,
    timeline_score_baseline,
    format_timestamp,
)

STOP_HOOK_EVENT_NAMES = ("Stop", "SubagentStop")


STOP_HOOK_DECISIONS = ("block", "allow", "skipped", "error", "unknown")


def pi_dispatch_usage(usage: Any) -> tuple[float | None, float | None]:
    if not isinstance(usage, dict):
        return None, None
    token_parts = [
        finite_float(usage.get(field))
        for field in ("input", "output", "cacheRead", "cacheWrite")
    ]
    processed_tokens = (
        sum(value for value in token_parts if value is not None)
        if any(value is not None for value in token_parts)
        else None
    )
    return processed_tokens, normalize_pi_cost(
        usage.get("costTotal"), processed_tokens=processed_tokens
    )


def _timeline_performance(
    task: dict[str, Any], timeline: dict[str, Any]
) -> dict[str, Any]:
    start_epoch = parse_epoch(timeline.get("start_at"))
    duration = finite_float(timeline.get("duration_seconds"))
    if start_epoch is None or duration is None:
        return {}

    statistics = task.get("statistics") or {}
    scores = statistics.get("scores") or {}
    direction = str(
        scores.get("direction")
        or (task.get("frozen_spec") or {}).get("metric_direction")
        or "maximize"
    )
    baseline, baseline_source = timeline_score_baseline(task)
    selected = finite_float(scores.get("selected"))
    checkpoints: list[dict[str, Any]] = []
    for candidate in task.get("candidates", []):
        for iteration in candidate.get("iterations", []):
            if iteration.get("process_passed") is not True:
                continue
            score = finite_float(iteration.get("score"))
            created_epoch = parse_epoch(iteration.get("created_at"))
            if score is None or created_epoch is None:
                continue
            checkpoints.append(
                {
                    "at": iteration.get("created_at"),
                    "epoch": created_epoch,
                    "score": score,
                    "candidate_id": candidate.get("candidate_id"),
                    "session_id": iteration.get("agent_session_id"),
                }
            )
    checkpoints.sort(key=lambda item: item["epoch"])
    current = baseline
    best_points: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        score = checkpoint["score"]
        if current is not None and not is_better_score(score, current, direction):
            continue
        current = score
        best_points.append(
            {key: value for key, value in checkpoint.items() if key != "epoch"}
        )

    worker_events = [
        event
        for event in timeline.get("events", [])
        if event.get("kind") == "worker_session"
    ]
    metric_keys = (
        "score_gain",
        "score_raw",
        "tokens_per_minute",
        "cost_per_minute",
        "verifier_density",
    )
    metric_ranges: dict[str, dict[str, float | int]] = {}
    for key in metric_keys:
        values = [
            value
            for event in worker_events
            if (value := finite_float(event.get(key))) is not None
        ]
        if values:
            metric_ranges[key] = {
                "min": min(values),
                "max": max(values),
                "observed": len(values),
            }

    spans = sorted(
        (start, end)
        for event in worker_events
        if (start := parse_epoch(event.get("start_at"))) is not None
        and (end := parse_epoch(event.get("end_at"))) is not None
        and end > start
    )
    merged: list[list[float]] = []
    for span_start, span_end in spans:
        if not merged or span_start > merged[-1][1]:
            merged.append([span_start, span_end])
        else:
            merged[-1][1] = max(merged[-1][1], span_end)
    idle_threshold = max(60.0, duration * 0.05)
    idle_intervals = [
        {
            "start_at": format_timestamp(previous[1]),
            "end_at": format_timestamp(following[0]),
            "duration_seconds": following[0] - previous[1],
        }
        for previous, following in zip(merged, merged[1:])
        if following[0] - previous[1] >= idle_threshold
    ]
    return {
        "metric_name": scores.get("metric_name")
        or (task.get("frozen_spec") or {}).get("metric_name"),
        "metric_direction": direction,
        "score": {
            "baseline": baseline,
            "baseline_source": baseline_source,
            "selected": selected,
            "points": best_points,
        },
        "metric_ranges": metric_ranges,
        "idle_intervals": idle_intervals,
        "max_parallel": ((task.get("frozen_spec") or {}).get("budget") or {}).get(
            "max_parallel"
        ),
    }


_GOAL_EVENT_LABELS = {
    "created": "Goal created",
    "session_activated": "Main-agent session attached",
    "triage_recorded": "Triage recorded",
    "spec_draft_saved": "Search specification drafted",
    "frozen_verifier_confirmed": "Verifier frozen",
    "search_linked": "Search task linked",
    "search_result_recorded": "Search result recorded",
    "goal_updated": "Goal revision created",
    "status_changed": "Goal status changed",
    "final_check_requested": "Final check requested",
    "final_check_submitted": "Final check completed",
}


def _goal_event_label(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "event")
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    base = _GOAL_EVENT_LABELS.get(event_type, event_type.replace("_", " ").title())
    if event_type in {"search_linked", "search_result_recorded"} and payload.get(
        "run_id"
    ):
        return f"{base}: {payload['run_id']}"
    if event_type == "status_changed" and payload.get("status"):
        return f"{base}: {payload['status']}"
    if event_type == "goal_updated" and payload.get("goal_revision"):
        return f"{base}: revision {payload['goal_revision']}"
    return base


def build_timeline(
    goal: GoalPlusRecord | None,
    goal_events: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    goal_timeline_events: list[dict[str, Any]] = []
    if goal is not None:
        goal_timeline_events.append(
            {
                "lane": "main",
                "kind": "main_span",
                "label": "Goal record activity window",
                "start_at": goal.created_at,
                "end_at": goal.updated_at,
                "inferred_end": False,
                "run_id": None,
            }
        )
    for event in goal_events:
        if event.get("event_type") not in _GOAL_EVENT_LABELS:
            continue
        created_at = event.get("created_at")
        if not isinstance(created_at, str):
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        goal_timeline_events.append(
            {
                "lane": "main",
                "kind": "milestone",
                "label": _goal_event_label(event),
                "start_at": created_at,
                "end_at": None,
                "inferred_end": False,
                "run_id": payload.get("run_id"),
            }
        )
    for task in tasks:
        run_id = task.get("run_id")
        task_events: list[dict[str, Any]] = []
        task_scores = (task.get("statistics") or {}).get("scores") or {}
        metric_direction = str(
            task_scores.get("direction")
            or (task.get("frozen_spec") or {}).get("metric_direction")
            or "maximize"
        )
        baseline_score, _ = timeline_score_baseline(task)
        session_scores = collect_session_scores(task, metric_direction)
        sessions_by_candidate: dict[str, list[str]] = {}
        for session in task.get("sessions", []):
            sessions_by_candidate.setdefault(
                str(session.get("candidate_id") or "unknown"), []
            ).append(
                str(
                    session.get("timeline_session_id")
                    or session.get("agent_session_id")
                    or "unknown"
                )
            )
        for session in task.get("sessions", []):
            start_at = session.get("started_at") or session.get("created_at")
            end_at = session.get("ended_at")
            duration_seconds = finite_float(session.get("duration_seconds"))
            inferred = False
            if end_at is None and duration_seconds is not None:
                start_epoch = parse_epoch(start_at)
                if start_epoch is not None:
                    end_at = format_timestamp(start_epoch + duration_seconds)
                    inferred = True
            if end_at is None:
                end_at = session.get("updated_at")
                inferred = True
            if duration_seconds is None:
                start_epoch = parse_epoch(start_at)
                end_epoch = parse_epoch(end_at)
                if (
                    start_epoch is not None
                    and end_epoch is not None
                    and end_epoch >= start_epoch
                ):
                    duration_seconds = end_epoch - start_epoch
            terminal = session.get("terminal_state") or "unknown"
            session_id = str(session["agent_session_id"])
            timeline_session_id = str(session.get("timeline_session_id") or session_id)
            candidate_id = str(session.get("candidate_id") or "unknown")
            candidate_sessions = sessions_by_candidate.get(
                candidate_id, [timeline_session_id]
            )
            attempt_index = candidate_sessions.index(timeline_session_id) + 1
            score = finite_float(session.get("score"))
            if score is None and "score" not in session:
                score = session_scores.get(session_id)
            score_improvement = None
            if score is not None and baseline_score is not None:
                score_improvement = (
                    baseline_score - score
                    if metric_direction == "minimize"
                    else score - baseline_score
                )
            task_events.append(
                {
                    "lane": "worker",
                    "kind": "worker_session",
                    "label": f"{timeline_session_id} / {terminal}",
                    "start_at": start_at,
                    "end_at": end_at,
                    "inferred_end": inferred,
                    "run_id": run_id,
                    "session_id": session_id,
                    "track_label": timeline_session_id,
                    "dispatch_index": session.get("dispatch_index"),
                    "dispatch_count": session.get("dispatch_count"),
                    "process_pid": session.get("process_pid"),
                    "candidate_id": candidate_id,
                    "terminal_state": terminal,
                    "duration_seconds": duration_seconds,
                    "processed_tokens": session.get("processed_tokens"),
                    "cost_usd": session.get("cost_usd"),
                    "tool_calls": session.get("tool_calls"),
                    "verifier_runs": session.get("verifier_runs"),
                    "tokens_per_minute": per_minute(
                        session.get("processed_tokens"), duration_seconds
                    ),
                    "cost_per_minute": per_minute(
                        session.get("cost_usd"), duration_seconds
                    ),
                    "verifier_density": per_minute(
                        session.get("verifier_runs"), duration_seconds
                    ),
                    "score": score,
                    "score_raw": score,
                    "score_gain": score_improvement,
                    "attempt_index": attempt_index,
                    "attempt_count": len(candidate_sessions),
                }
            )
        for candidate in task.get("candidates", []):
            for iteration in candidate.get("iterations", []):
                parent_owned = iteration.get("agent_session_id") is None
                task_events.append(
                    {
                        "lane": "verifier",
                        "kind": (
                            "parent_verifier" if parent_owned else "worker_verifier"
                        ),
                        "label": (
                            f"Parent verifier {candidate['candidate_id']} #{iteration['iteration']}"
                            if parent_owned
                            else f"Worker verifier {candidate['candidate_id']} #{iteration['iteration']}"
                        ),
                        "start_at": iteration.get("created_at"),
                        "end_at": None,
                        "inferred_end": False,
                        "run_id": run_id,
                        "session_id": iteration.get("agent_session_id"),
                        "score": iteration.get("score"),
                    }
                )
        for candidate in task.get("candidates", []):
            if not candidate.get("promotion_passed"):
                continue
            run = task.get("run") or {}
            promotion_at = (
                candidate.get("promotion_evidence_at")
                or task.get("result_recorded_at")
                or run.get("created_at")
            )
            task_events.append(
                {
                    "lane": "verifier",
                    "kind": "promotion",
                    "label": f"Promotion passed: {candidate['candidate_id']}",
                    "start_at": promotion_at,
                    "end_at": None,
                    "inferred_end": candidate.get("promotion_evidence_at") is None,
                    "run_id": run_id,
                }
            )

        run = task.get("run") or {}
        run_started_at = run.get("created_at")
        task_epochs = [
            value
            for event in task_events
            for value in (parse_epoch(event.get("start_at")), parse_epoch(event.get("end_at")))
            if value is not None
        ]
        run_started_epoch = parse_epoch(run_started_at)
        task_end_epoch = max(task_epochs) if task_epochs else run_started_epoch
        if run_started_epoch is not None and task_end_epoch is not None:
            task_events.insert(
                0,
                {
                    "lane": "main",
                    "kind": "main_span",
                    "label": "Search orchestration",
                    "start_at": run_started_at,
                    "end_at": format_timestamp(max(run_started_epoch + 1.0, task_end_epoch)),
                    "inferred_end": False,
                    "run_id": run_id,
                },
            )
        task_timeline = _timeline_payload(task_events)
        task_timeline["performance"] = _timeline_performance(task, task_timeline)
        task["timeline"] = task_timeline

    gate_counts = Counter(
        str(event.get("event_type"))
        for event in goal_events
        if event.get("event_type") in {"gate_allowed", "gate_blocked"}
    )
    return _timeline_payload(
        goal_timeline_events,
        gate_events=dict(sorted(gate_counts.items())),
    )


def _timeline_payload(
    events: list[dict[str, Any]],
    *,
    gate_events: dict[str, int] | None = None,
) -> dict[str, Any]:
    epochs = [
        value
        for event in events
        for value in (parse_epoch(event.get("start_at")), parse_epoch(event.get("end_at")))
        if value is not None
    ]
    start_epoch = min(epochs) if epochs else None
    end_epoch = max(epochs) if epochs else None
    duration = (
        max(1.0, end_epoch - start_epoch)
        if start_epoch is not None and end_epoch is not None
        else None
    )
    events.sort(key=lambda event: parse_epoch(event.get("start_at")) or float("inf"))
    return {
        "start_at": format_timestamp(start_epoch),
        "end_at": format_timestamp(end_epoch),
        "duration_seconds": duration,
        "events": events,
        "gate_events": gate_events or {},
    }


def stop_hook_decision_counts() -> dict[str, int]:
    return {decision: 0 for decision in STOP_HOOK_DECISIONS}


def _normalized_stop_hook_event(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    event_name = payload.get("hook_event_name")
    if event_name not in STOP_HOOK_EVENT_NAMES:
        return None
    decision = payload.get("decision")
    if decision not in STOP_HOOK_DECISIONS:
        decision = "unknown"
    duration_ms = payload.get("duration_ms")
    if (
        not isinstance(duration_ms, int | float)
        or isinstance(duration_ms, bool)
        or not isfinite(duration_ms)
        or duration_ms < 0
    ):
        duration_ms = None
    text_fields = (
        "invocation_id",
        "started_at",
        "finished_at",
        "outcome",
        "reason",
        "goal_plus_id",
        "session_id",
        "host_agent_id",
        "agent_session_id",
        "run_id",
        "candidate_id",
        "stop_reason",
        "error_type",
        "error",
    )
    event = {
        key: value if isinstance((value := payload.get(key)), str) and value else None
        for key in text_fields
    }
    event.update(
        {
            "schema_version": payload.get("schema_version"),
            "hook_event_name": event_name,
            "decision": decision,
            "duration_ms": duration_ms,
        }
    )
    return event


def _activity_window_counts(
    events: list[dict[str, Any]],
    *,
    start_epoch: float | None = None,
    end_epoch: float | None = None,
) -> dict[str, Any]:
    tool_counts: Counter[str] = Counter()
    message_counts: Counter[str] = Counter()
    context_samples: list[tuple[float, float]] = []
    for event in events:
        event_epoch = parse_epoch(event.get("at"))
        if event_epoch is None:
            continue
        if start_epoch is not None and event_epoch < start_epoch:
            continue
        if end_epoch is not None and event_epoch >= end_epoch:
            continue
        kind = event.get("kind")
        if kind == "tool_call":
            tool_counts[str(event.get("category") or "other")] += 1
        elif kind == "assistant_message":
            message_counts[str(event.get("classification") or "substantive")] += 1
            if event.get("duplicate") is True:
                message_counts["duplicate"] += 1
        elif kind == "context":
            percent = finite_float(event.get("percent"))
            if percent is not None:
                context_samples.append((event_epoch, percent))
    return {
        "tool_calls": sum(tool_counts.values()),
        "context_calls": tool_counts.get("context", 0),
        "global_evidence_calls": tool_counts.get("global_evidence", 0),
        "verifier_tool_calls": tool_counts.get("verifier", 0),
        "iteration_plan_calls": tool_counts.get("iteration_plan", 0),
        "edit_capable_calls": tool_counts.get("edit_capable", 0),
        "other_tool_calls": tool_counts.get("other", 0),
        "assistant_messages": sum(
            count
            for classification, count in message_counts.items()
            if classification != "duplicate"
        ),
        "empty_messages": message_counts.get("empty", 0),
        "best_remains_messages": message_counts.get("best_remains", 0),
        "cannot_continue_messages": message_counts.get("cannot_continue", 0),
        "substantive_messages": message_counts.get("substantive", 0),
        "duplicate_messages": message_counts.get("duplicate", 0),
        "context_samples": len(context_samples),
        "context_percent_final": (
            max(context_samples, key=lambda item: item[0])[1]
            if context_samples
            else None
        ),
        "context_percent_max": (
            max(value for _, value in context_samples) if context_samples else None
        ),
    }


def _nearest_context_percent(
    events: list[dict[str, Any]],
    target_epoch: float | None,
) -> float | None:
    if target_epoch is None:
        return None
    samples = [
        (abs(event_epoch - target_epoch), percent)
        for event in events
        if event.get("kind") == "context"
        and (event_epoch := parse_epoch(event.get("at"))) is not None
        and (percent := finite_float(event.get("percent"))) is not None
    ]
    return min(samples, default=(0.0, None), key=lambda item: item[0])[1]


def _loop_tail_signal(
    *,
    activity_available: bool,
    last_verifier_at: str | None,
    post_last: dict[str, Any],
) -> str:
    if not activity_available:
        return "unavailable"
    if last_verifier_at is None:
        return "no-verifier"
    messages = int(post_last.get("assistant_messages") or 0)
    empty = int(post_last.get("empty_messages") or 0)
    best_remains = int(post_last.get("best_remains_messages") or 0)
    cannot_continue = int(post_last.get("cannot_continue_messages") or 0)
    context_reads = int(post_last.get("context_calls") or 0)
    evidence_reads = int(post_last.get("global_evidence_calls") or 0)
    edits = int(post_last.get("edit_capable_calls") or 0)
    tool_calls = int(post_last.get("tool_calls") or 0)
    if messages >= 5 and empty * 5 >= messages * 4:
        return "empty-output"
    if context_reads + evidence_reads >= 10 and edits == 0:
        return "polling-only"
    if (
        messages >= 3
        and best_remains >= 2
        and best_remains * 2 >= messages
        and edits == 0
        and tool_calls <= 1
    ):
        return "answer-only"
    if cannot_continue > 0 and edits == 0:
        return "cannot-continue"
    if edits > 0:
        return "unverified-edit"
    if messages > 0 or tool_calls > 0:
        return "unverified-tail"
    return "quiet"


def build_loop_agent_statistics(
    tasks: list[dict[str, Any]],
    stop_hook_statistics: dict[str, Any],
) -> dict[str, Any]:
    sessions: dict[tuple[str, str], dict[str, Any]] = {}
    candidate_sessions: dict[tuple[str, str], set[str]] = {}
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    task_by_run: dict[str, dict[str, Any]] = {}
    for task in tasks:
        run_id = task.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        task_by_run[run_id] = task
        for candidate in task.get("candidates") or []:
            candidate_id = candidate.get("candidate_id")
            if isinstance(candidate_id, str) and candidate_id:
                candidates[(run_id, candidate_id)] = candidate
        for session in task.get("sessions") or []:
            if session.get("agent_harness") != "codex":
                continue
            agent_session_id = session.get("agent_session_id")
            candidate_id = session.get("candidate_id")
            if not isinstance(agent_session_id, str) or not agent_session_id:
                continue
            if not isinstance(candidate_id, str) or not candidate_id:
                continue
            sessions.setdefault((run_id, agent_session_id), session)
            candidate_sessions.setdefault((run_id, candidate_id), set()).add(
                agent_session_id
            )

    host_agent_sessions: dict[str, str] = {}
    for summary in stop_hook_statistics.get("subagents") or []:
        agent_session_id = summary.get("agent_session_id")
        if not isinstance(agent_session_id, str) or not agent_session_id:
            continue
        for host_agent_id in summary.get("host_agent_ids") or []:
            if isinstance(host_agent_id, str) and host_agent_id:
                host_agent_sessions[host_agent_id] = agent_session_id

    events_by_session: dict[tuple[str, str], list[dict[str, Any]]] = {}
    subagent_hook_streams: set[str] = set()
    for raw_event in stop_hook_statistics.get("events") or []:
        if raw_event.get("hook_event_name") != "SubagentStop":
            continue
        event = dict(raw_event)
        run_id = event.get("run_id")
        candidate_id = event.get("candidate_id")
        if isinstance(run_id, str) and run_id:
            subagent_hook_streams.add(run_id)
        agent_session_id = event.get("agent_session_id")
        host_agent_id = event.get("host_agent_id")
        if not isinstance(agent_session_id, str) and isinstance(host_agent_id, str):
            agent_session_id = host_agent_sessions.get(host_agent_id)
        if (
            not isinstance(agent_session_id, str)
            and isinstance(run_id, str)
            and isinstance(candidate_id, str)
        ):
            candidates_for_event = candidate_sessions.get((run_id, candidate_id), set())
            if len(candidates_for_event) == 1:
                agent_session_id = next(iter(candidates_for_event))
        if not isinstance(run_id, str) or not isinstance(agent_session_id, str):
            continue
        event["resolved_agent_session_id"] = agent_session_id
        events_by_session.setdefault((run_id, agent_session_id), []).append(event)
    for events in events_by_session.values():
        events.sort(
            key=lambda event: (
                parse_epoch(event.get("started_at")) or float("inf"),
                str(event.get("invocation_id") or ""),
            )
        )

    rows: list[dict[str, Any]] = []
    stop_windows: list[dict[str, Any]] = []
    for (run_id, agent_session_id), session in sorted(sessions.items()):
        task = task_by_run[run_id]
        candidate_id = str(session.get("candidate_id"))
        candidate = candidates.get((run_id, candidate_id)) or {}
        activity = session.get("activity")
        activity = activity if isinstance(activity, dict) else {}
        activity_available = activity.get("available") is True
        activity_events = activity.get("events")
        activity_events = (
            [event for event in activity_events if isinstance(event, dict)]
            if isinstance(activity_events, list)
            else []
        )
        direction = str(
            (task.get("frozen_spec") or {}).get("metric_direction") or "maximize"
        )
        candidate_session_ids = candidate_sessions.get((run_id, candidate_id), set())
        candidate_iterations = candidate.get("iterations") or []
        has_session_attribution = any(
            isinstance(iteration, dict)
            and isinstance(iteration.get("agent_session_id"), str)
            for iteration in candidate_iterations
        )
        iteration_rows = []
        current_best: float | None = None
        previous_git_head: str | None = None
        for iteration in sorted(
            candidate_iterations,
            key=lambda item: parse_epoch(item.get("created_at")) or float("inf"),
        ):
            iteration_session_id = iteration.get("agent_session_id")
            belongs_to_session = iteration_session_id == agent_session_id or (
                not has_session_attribution
                and iteration_session_id is None
                and len(candidate_session_ids) == 1
            )
            if not belongs_to_session:
                continue
            item = dict(iteration)
            git_head = item.get("git_head")
            item["_revision_changed"] = bool(
                isinstance(git_head, str) and git_head and git_head != previous_git_head
            )
            if isinstance(git_head, str) and git_head:
                previous_git_head = git_head
            score = finite_float(item.get("score"))
            improved = False
            if item.get("process_passed") is True and score is not None:
                improved = current_best is None or is_better_score(
                    score, current_best, direction
                )
                if improved:
                    current_best = score
            item["_improved"] = improved
            iteration_rows.append(item)

        last_verifier = max(
            (
                iteration.get("created_at")
                for iteration in iteration_rows
                if parse_epoch(iteration.get("created_at")) is not None
            ),
            key=lambda value: parse_epoch(value) or float("-inf"),
            default=None,
        )
        last_verifier_epoch = parse_epoch(last_verifier)
        total_activity = _activity_window_counts(activity_events)
        post_last = _activity_window_counts(
            activity_events,
            start_epoch=last_verifier_epoch,
        )
        session_events = events_by_session.get((run_id, agent_session_id), [])
        blocked_events = [
            event for event in session_events if event.get("decision") == "block"
        ]
        for index, event in enumerate(session_events):
            start_at = event.get("finished_at") or event.get("started_at")
            start_epoch = parse_epoch(start_at)
            next_event = (
                session_events[index + 1] if index + 1 < len(session_events) else None
            )
            end_at = next_event.get("started_at") if next_event is not None else None
            end_epoch = parse_epoch(end_at)
            window_activity = _activity_window_counts(
                activity_events,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
            )
            window_iterations = [
                iteration
                for iteration in iteration_rows
                if (iteration_epoch := parse_epoch(iteration.get("created_at"))) is not None
                and (start_epoch is None or iteration_epoch >= start_epoch)
                and (end_epoch is None or iteration_epoch < end_epoch)
            ]
            revision_changes = sum(
                iteration.get("_revision_changed") is True
                for iteration in window_iterations
            )
            improvements = sum(
                iteration.get("_improved") is True for iteration in window_iterations
            )
            if event.get("decision") != "block":
                outcome = "terminal-allow"
            elif window_iterations and revision_changes:
                outcome = "productive"
            elif window_iterations:
                outcome = "verified-no-revision"
            elif (
                window_activity["context_calls"]
                + window_activity["global_evidence_calls"]
            ):
                outcome = "polling-only"
            elif window_activity["assistant_messages"]:
                if (
                    window_activity["empty_messages"] * 5
                    >= window_activity["assistant_messages"] * 4
                ):
                    outcome = "empty-output"
                elif (
                    window_activity["best_remains_messages"]
                    or window_activity["cannot_continue_messages"]
                    or window_activity["duplicate_messages"]
                ):
                    outcome = "answer-only"
                else:
                    outcome = "unverified-output"
            elif window_activity["tool_calls"]:
                outcome = "unverified-tooling"
            else:
                outcome = "no-observed-followup"
            stop_windows.append(
                {
                    "run_id": run_id,
                    "candidate_id": candidate_id,
                    "agent_session_id": agent_session_id,
                    "stop_index": index + 1,
                    "started_at": event.get("started_at"),
                    "decision": event.get("decision"),
                    "verifier_runs": len(window_iterations),
                    "verified_revision_changes": revision_changes,
                    "improvements": improvements,
                    **window_activity,
                    "outcome": outcome,
                }
            )

        blocked_windows = [
            window
            for window in stop_windows
            if window["run_id"] == run_id
            and window["agent_session_id"] == agent_session_id
            and window["decision"] == "block"
        ]
        first_block_epoch = (
            parse_epoch(blocked_events[0].get("started_at")) if blocked_events else None
        )
        context_at_first_block = _nearest_context_percent(
            activity_events, first_block_epoch
        )
        context_final = total_activity.get("context_percent_final")
        context_growth = (
            context_final - context_at_first_block
            if context_final is not None and context_at_first_block is not None
            else None
        )
        hook_stream_available = run_id in subagent_hook_streams
        decision_counts = Counter(
            str(event.get("decision") or "unknown") for event in session_events
        )
        rows.append(
            {
                "run_id": run_id,
                "candidate_id": candidate_id,
                "agent_session_id": agent_session_id,
                "selected": candidate.get("selected") is True,
                "best_score": candidate.get("best_score"),
                "hook_stream_available": hook_stream_available,
                "stop_calls": len(session_events) if hook_stream_available else None,
                "blocked_stops": (
                    decision_counts.get("block", 0) if hook_stream_available else None
                ),
                "allowed_stops": (
                    decision_counts.get("allow", 0) if hook_stream_available else None
                ),
                "productive_blocked_stops": (
                    sum(window["outcome"] == "productive" for window in blocked_windows)
                    if hook_stream_available
                    else None
                ),
                "verifier_runs_after_blocked_stops": (
                    sum(window["verifier_runs"] for window in blocked_windows)
                    if hook_stream_available
                    else None
                ),
                "verified_revision_changes_after_blocked_stops": (
                    sum(
                        window["verified_revision_changes"]
                        for window in blocked_windows
                    )
                    if hook_stream_available
                    else None
                ),
                "improvements_after_blocked_stops": (
                    sum(window["improvements"] for window in blocked_windows)
                    if hook_stream_available
                    else None
                ),
                "activity_available": activity_available,
                "activity": total_activity,
                "last_verifier_at": last_verifier,
                "post_last_verifier": post_last,
                "context_percent_at_first_block_nearest": context_at_first_block,
                "context_percent_final": context_final,
                "context_percent_max": total_activity.get("context_percent_max"),
                "context_growth_after_first_block": context_growth,
                "tail_signal": _loop_tail_signal(
                    activity_available=activity_available,
                    last_verifier_at=last_verifier,
                    post_last=post_last,
                ),
            }
        )

    return {
        "schema_version": 1,
        "rows": rows,
        "stop_windows": stop_windows,
        "definitions": {
            "productive_stop": (
                "A blocked SubagentStop followed before the next stop by a "
                "verifier on a different immutable Git revision."
            ),
            "polling_only": (
                "At least ten context/global-evidence reads after the last "
                "verifier, with no edit-capable call."
            ),
            "answer_only": (
                "At least three post-verifier assistant messages, at least "
                "half classified as short best-remains replies, and at most "
                "one tool call."
            ),
            "empty_output": (
                "At least five post-verifier assistant messages, at least "
                "80 percent empty."
            ),
        },
    }


def build_stop_hook_statistics(
    raw_events: list[Any],
    *,
    source_available: bool,
    source_path: str,
    goal_plus_id: str | None,
    run_ids: set[str],
) -> dict[str, Any]:
    events = []
    for payload in raw_events:
        event = _normalized_stop_hook_event(payload)
        if event is None:
            continue
        event_goal_id = event.get("goal_plus_id")
        matches_goal = goal_plus_id is not None and event_goal_id == goal_plus_id
        matches_run = event.get("run_id") in run_ids
        if goal_plus_id is not None:
            included = matches_goal or (event_goal_id is None and matches_run)
        else:
            included = matches_run
        if not included:
            continue
        events.append(event)

    events.sort(
        key=lambda event: (
            parse_epoch(event.get("started_at")) or float("inf"),
            str(event.get("invocation_id") or ""),
        )
    )
    by_decision = stop_hook_decision_counts()
    by_event = {
        event_name: {
            "events_total": 0,
            "duration_ms_total": 0.0,
            "decisions": stop_hook_decision_counts(),
        }
        for event_name in STOP_HOOK_EVENT_NAMES
    }
    subagents: dict[str, dict[str, Any]] = {}
    host_agent_sessions = {
        str(event["host_agent_id"]): str(event["agent_session_id"])
        for event in events
        if event["hook_event_name"] == "SubagentStop"
        and event.get("host_agent_id")
        and event.get("agent_session_id")
    }
    duration_ms_total = 0.0
    for event in events:
        event_name = str(event["hook_event_name"])
        decision = str(event["decision"])
        duration_ms = float(event.get("duration_ms") or 0.0)
        duration_ms_total += duration_ms
        by_decision[decision] += 1
        by_event[event_name]["events_total"] += 1
        by_event[event_name]["duration_ms_total"] += duration_ms
        by_event[event_name]["decisions"][decision] += 1
        if event_name != "SubagentStop":
            continue
        agent_session_id = event.get("agent_session_id")
        host_agent_id = event.get("host_agent_id")
        if not agent_session_id and host_agent_id:
            agent_session_id = host_agent_sessions.get(str(host_agent_id))
        if agent_session_id:
            identity = str(agent_session_id)
            identity_source = "agent_session_id"
        elif host_agent_id:
            identity = str(host_agent_id)
            identity_source = "host_agent_id"
        else:
            identity = "unresolved"
            identity_source = "unresolved"
        key = f"{identity_source}:{identity}"
        summary = subagents.setdefault(
            key,
            {
                "identity": identity,
                "identity_source": identity_source,
                "agent_session_id": agent_session_id,
                "host_agent_ids": set(),
                "run_ids": set(),
                "candidate_ids": set(),
                "events_total": 0,
                "duration_ms_total": 0.0,
                "decisions": stop_hook_decision_counts(),
                "last_event_at": None,
            },
        )
        for field, target in (
            ("host_agent_id", "host_agent_ids"),
            ("run_id", "run_ids"),
            ("candidate_id", "candidate_ids"),
        ):
            value = event.get(field)
            if isinstance(value, str) and value:
                summary[target].add(value)
        summary["events_total"] += 1
        summary["duration_ms_total"] += duration_ms
        summary["decisions"][decision] += 1
        observed_at = event.get("finished_at") or event.get("started_at")
        if observed_at and (
            summary["last_event_at"] is None
            or (parse_epoch(observed_at) or float("-inf"))
            > (parse_epoch(summary["last_event_at"]) or float("-inf"))
        ):
            summary["last_event_at"] = observed_at

    subagent_rows = []
    for summary in subagents.values():
        subagent_rows.append(
            {
                **summary,
                "host_agent_ids": sorted(summary["host_agent_ids"]),
                "run_ids": sorted(summary["run_ids"]),
                "candidate_ids": sorted(summary["candidate_ids"]),
                "duration_ms_total": round(summary["duration_ms_total"], 3),
            }
        )
    subagent_rows.sort(key=lambda item: (item["identity_source"], item["identity"]))
    captured_through = max(
        (
            observed_at
            for event in events
            if (observed_at := event.get("finished_at") or event.get("started_at"))
        ),
        key=lambda value: parse_epoch(value) or float("-inf"),
        default=None,
    )
    for summary in by_event.values():
        summary["duration_ms_total"] = round(summary["duration_ms_total"], 3)
    return {
        "schema_version": 1,
        "source_available": source_available,
        "source_path": source_path,
        "events_total": len(events),
        "duration_ms_total": round(duration_ms_total, 3),
        "captured_through": captured_through,
        "by_event": by_event,
        "by_decision": by_decision,
        "subagents": subagent_rows,
        "events": events,
    }
