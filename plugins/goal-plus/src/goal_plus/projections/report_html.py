from __future__ import annotations

import json

from collections import Counter


from html import escape

from math import floor, log10

from pathlib import Path

from typing import Any

from goal_plus._version import __version__
from goal_plus.projections.report_assets import REPORT_CSS, REPORT_SCRIPT
from goal_plus.projections.report_format import (
    format_bytes,
    format_cost,
    format_duration,
    parse_epoch,
    finite_float,
    escape_html,
    is_better_score,
    format_milliseconds,
    numeric_value,
    format_percent,
    reported_view_state,
    render_status,
    status_class,
    format_text,
    format_timestamp,
)
from goal_plus.projections.report_statistics import stop_hook_decision_counts

REPORT_SCHEMA_VERSION = 1


REPORT_DOCUMENT_SCHEMA_VERSION = 1


def _metric_card(label: str, value: str, detail: str = "", tone: str = "") -> str:
    return (
        '<div class="kpi">'
        f'<div class="kpi-label">{escape(label)}</div>'
        f'<div class="metric-value {tone}">{escape(value)}</div>'
        f'<div class="kpi-detail">{escape(detail)}</div>'
        "</div>"
    )


_METRIC_GAP_INFO = {
    "target_score": (
        "Not configured",
        "No score threshold was declared. This does not mean the Goal Plus task failed.",
    ),
    "baseline_score": (
        "Not configured",
        "No baseline score was supplied for improvement analysis.",
    ),
    "orchestrator_cost_usd": (
        "Not collected",
        "The attached main-agent evidence does not expose a reliable orchestrator cost.",
    ),
    "orchestrator_token_usage": (
        "Not collected",
        "No readable main-agent transcript usage record was attached.",
    ),
    "orchestrator_usage_breakdown": (
        "Not collected",
        "Detailed main-agent input, cache, output, and tool usage are not available.",
    ),
    "hardware_utilization": (
        "Not collected",
        "Worker host CPU, GPU, memory, and accelerator telemetry are outside the current evidence contract.",
    ),
    "worker_time_to_first_token": (
        "Not collected",
        "The worker host did not publish time-to-first-token for every session.",
    ),
    "worker_processed_tokens": (
        "Partial coverage",
        "Processed-token usage is missing for one or more worker sessions.",
    ),
    "worker_cost_usd": (
        "Partial coverage",
        "A model-rate cost estimate is missing for one or more worker sessions.",
    ),
    "worker_duration": (
        "Partial coverage",
        "Observed duration is missing for one or more worker sessions.",
    ),
    "semantic_candidate_coverage": (
        "Not computed",
        "Candidate semantic diversity is not currently scored.",
    ),
    "redundant_attempt_rate": (
        "Not computed",
        "The report does not classify semantically duplicate attempts.",
    ),
    "temporal_collision_rate": (
        "Not computed",
        "The report does not classify concurrent workers as colliding or duplicating work.",
    ),
    "research_rollup_quality": (
        "Not computed",
        "No verifier-backed quality metric exists for the final research synthesis.",
    ),
    "normalized_score": (
        "Not computed",
        "The verifier score is reported in its native scale; no cross-task normalization is declared.",
    ),
    "promotion_attempt_history": (
        "Not retained",
        "The durable report has final promotion evidence, not a complete history of every attempted promotion.",
    ),
}


def _render_metric_availability(items: list[Any]) -> str:
    names = list(dict.fromkeys(str(item) for item in items if item))
    if not names:
        return "<p>No known metric availability gaps.</p>"
    rows = []
    for name in names:
        kind, reason = _METRIC_GAP_INFO.get(
            name,
            (
                "Not observed",
                "This value was not present in the durable report evidence.",
            ),
        )
        rows.append(
            "<li>"
            f"<code>{escape(name)}</code>"
            f'<span class="metric-gap-kind">{escape(kind)}</span>'
            f'<span class="metric-gap-reason">{escape(reason)}</span>'
            "</li>"
        )
    return (
        '<details class="summary-block">'
        f"<summary>Metric availability ({len(names)} gaps)</summary>"
        f'<div><ul class="metric-gap-list">{"".join(rows)}</ul></div>'
        "</details>"
    )


def _stat_rows(values: dict[str, Any], formatters: dict[str, Any] | None = None) -> str:
    formatters = formatters or {}
    rows = []
    for key, value in values.items():
        formatter = formatters.get(key, format_text)
        rows.append(
            '<div class="stat-row">'
            f"<span>{escape(key.replace('_', ' ').title())}</span>"
            f'<strong class="mono">{escape(formatter(value))}</strong>'
            "</div>"
        )
    return "".join(rows)


def _timeline_position(
    event: dict[str, Any], start_epoch: float, duration: float
) -> tuple[float, float]:
    event_start = parse_epoch(event.get("start_at")) or start_epoch
    event_end = parse_epoch(event.get("end_at"))
    left = max(0.0, min(99.0, (event_start - start_epoch) / duration * 100))
    if event_end is None:
        return left, 0.8
    width = max(1.0, (event_end - event_start) / duration * 100)
    return left, min(width, 100.0 - left)


def _timeline_width(duration_seconds: float) -> int:
    duration_minutes = max(0.0, duration_seconds / 60.0)
    return max(980, min(20_000, int(round(190 + duration_minutes * 80))))


def _metric_level(value: Any, metric_range: dict[str, Any]) -> int | None:
    number = finite_float(value)
    low = finite_float(metric_range.get("min"))
    high = finite_float(metric_range.get("max"))
    if number is None or low is None or high is None:
        return None
    ratio = 0.5 if high == low else (number - low) / (high - low)
    return min(4, max(1, int(ratio * 4) + 1))


def _metric_readout(metric: str, value: Any) -> str:
    number = finite_float(value)
    if number is None:
        return "Not observed"
    if metric == "score_gain":
        return f"{number:+.4f}".rstrip("0").rstrip(".")
    if metric == "score_raw":
        return numeric_value(number, digits=4)
    if metric == "tokens_per_minute":
        return f"{numeric_value(number, digits=0)}/min"
    if metric == "cost_per_minute":
        return f"${number:.4f}/min"
    if metric == "verifier_density":
        return f"{number:.1f}/min"
    return numeric_value(number)


def _render_score_chart(
    performance: dict[str, Any],
    start_epoch: float,
    duration: float,
) -> str:
    score_data = performance.get("score") or {}
    baseline = finite_float(score_data.get("baseline"))
    selected = finite_float(score_data.get("selected"))
    points = [
        point
        for point in score_data.get("points", [])
        if finite_float(point.get("score")) is not None
        and parse_epoch(point.get("at")) is not None
    ]
    values = [value for value in (baseline, selected) if value is not None]
    values.extend(float(point["score"]) for point in points)
    if not values:
        return ""

    low = min(values)
    high = max(values)
    padding = max((high - low) * 0.15, abs(high) * 0.02, 0.01)
    plot_low = low - padding
    plot_high = high + padding

    def y_position(value: float) -> float:
        return 54.0 - (value - plot_low) / (plot_high - plot_low) * 44.0

    current = baseline if baseline is not None else float(points[0]["score"])
    path_parts = [f"M 0 {y_position(current):.2f}"]
    point_marks = []
    for point in points:
        point_epoch = parse_epoch(point.get("at"))
        score = float(point["score"])
        if point_epoch is None:
            continue
        x = max(0.0, min(1000.0, (point_epoch - start_epoch) / duration * 1000.0))
        y = y_position(score)
        path_parts.extend((f"H {x:.2f}", f"V {y:.2f}"))
        tooltip = (
            f"{point.get('candidate_id') or 'candidate'}: {numeric_value(score, digits=4)} "
            f"at {point.get('at')}"
        )
        point_marks.append(
            f'<circle class="score-point" cx="{x:.2f}" cy="{y:.2f}" r="3" '
            f"<title>{escape(tooltip)}</title></circle>"
        )
    path_parts.append("H 1000")
    reference_lines = []
    reference_labels = []
    for label, value in (("Baseline", baseline), ("Selected", selected)):
        if value is None:
            continue
        y = y_position(value)
        reference_lines.append(
            f'<line class="score-reference" x1="0" y1="{y:.2f}" x2="1000" y2="{y:.2f}" />'
        )
        label_top = min(51.0, max(1.0, y - 11.0))
        reference_labels.append(
            f'<span class="score-ref-label" style="top:{label_top:.2f}px">'
            f"{escape(label)} {escape_html(numeric_value(value, digits=4))}</span>"
        )
    metric_name = str(performance.get("metric_name") or "score")
    baseline_summary = (
        numeric_value(baseline, digits=4) if baseline is not None else "No baseline"
    )
    summary = f"{baseline_summary} to {numeric_value(selected, digits=4)}"
    return (
        '<div class="score-row">'
        '<div class="score-label">'
        "<strong>Best score</strong>"
        f"<span>{escape(metric_name)} / {escape(summary)}</span>"
        "</div>"
        f'<div class="score-track" role="img" aria-label="Best score progression: {escape(summary, quote=True)}">'
        '<svg viewBox="0 0 1000 64" preserveAspectRatio="none" aria-hidden="true">'
        f"{''.join(reference_lines)}"
        f'<path class="score-step" d="{" ".join(path_parts)}" />'
        f"{''.join(point_marks)}"
        f"</svg>{''.join(reference_labels)}</div></div>"
    )


def _nice_trajectory_tick(raw_step: float) -> int:
    if raw_step <= 1:
        return 1
    magnitude = 10 ** floor(log10(raw_step))
    fraction = raw_step / magnitude
    nice_fraction = (
        1 if fraction <= 1 else 2 if fraction <= 2 else 5 if fraction <= 5 else 10
    )
    return max(1, int(nice_fraction * magnitude))


def _trajectory_call_window(evaluations: int) -> dict[str, int]:
    if evaluations <= 0:
        return {"start": 0, "end": 0, "tick": 1, "marker_size": 7}
    marker_size = 7 if evaluations <= 80 else 5 if evaluations <= 250 else 4
    return {
        "start": 0,
        "end": evaluations,
        "tick": _nice_trajectory_tick(max(1.0, evaluations / 12)),
        "marker_size": marker_size,
    }


def _trajectory_score_axis(scores: list[float]) -> dict[str, Any]:
    if not scores:
        return {
            "type": "linear",
            "range": None,
            "minimum": None,
            "maximum": None,
            "ratio": None,
        }
    minimum = min(scores)
    maximum = max(scores)
    ratio = maximum / minimum if minimum > 0 else None
    use_log = ratio is not None and ratio >= 20
    if use_log:
        low = log10(minimum)
        high = log10(maximum)
        span = max(high - low, 0.1)
        padding = max(0.06, span * 0.08)
        axis_range = [low - padding, high + padding]
    else:
        span = maximum - minimum
        padding = max(span * 0.08, abs(maximum) * 0.04, 1e-9)
        axis_range = [minimum - padding, maximum + padding]
    return {
        "type": "log" if use_log else "linear",
        "range": axis_range,
        "minimum": minimum,
        "maximum": maximum,
        "ratio": ratio,
    }


def _search_trajectory_payload(task: dict[str, Any]) -> dict[str, Any] | None:
    statistics = task.get("statistics") or {}
    scores = statistics.get("scores") or {}
    frozen = task.get("frozen_spec") or {}
    metric_name = str(scores.get("metric_name") or frozen.get("metric_name") or "score")
    metric_name = metric_name.replace("<", "").replace(">", "")
    direction = str(
        scores.get("direction") or frozen.get("metric_direction") or "maximize"
    )
    baseline = finite_float(scores.get("baseline"))
    selected_score = finite_float(scores.get("selected"))
    evaluations: list[dict[str, Any]] = []
    candidates = task.get("candidates") or []
    for candidate_index, candidate in enumerate(candidates):
        candidate_id = str(
            candidate.get("candidate_id") or f"candidate-{candidate_index + 1}"
        )
        for iteration_index, iteration in enumerate(candidate.get("iterations") or []):
            score = finite_float(iteration.get("score"))
            created_at = iteration.get("created_at")
            created_epoch = parse_epoch(created_at)
            session_id = iteration.get("agent_session_id")
            evaluations.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_index": candidate_index,
                    "selected": bool(candidate.get("selected")),
                    "iteration": iteration.get("iteration") or iteration_index + 1,
                    "score": score,
                    "process_passed": iteration.get("process_passed") is True,
                    "eligible": (
                        iteration.get("process_passed") is True
                        and score is not None
                        and iteration.get("disposition") != "failure"
                    ),
                    "failure_class": iteration.get("failure_class"),
                    "created_at": created_at,
                    "created_epoch": created_epoch,
                    "source": (
                        "worker verifier"
                        if session_id is not None
                        else "parent verifier"
                    ),
                    "fallback_order": iteration_index,
                }
            )
    if not evaluations:
        return None

    evaluations.sort(
        key=lambda item: (
            item["created_epoch"] is None,
            item["created_epoch"] if item["created_epoch"] is not None else 0.0,
            item["candidate_index"],
            item["fallback_order"],
        )
    )
    for call, evaluation in enumerate(evaluations, start=1):
        evaluation["call"] = call

    trajectories: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(candidates):
        candidate_id = str(
            candidate.get("candidate_id") or f"candidate-{candidate_index + 1}"
        )
        points = [
            evaluation
            for evaluation in evaluations
            if evaluation["candidate_id"] == candidate_id
        ]
        if not points:
            continue
        passing_points = [point for point in points if point["eligible"]]
        failed_points = [point for point in points if not point["eligible"]]
        trajectories.append(
            {
                "candidate_id": candidate_id,
                "selected": bool(candidate.get("selected")),
                "calls": [point["call"] for point in passing_points],
                "scores": [point["score"] for point in passing_points],
                "details": [
                    [
                        point["iteration"],
                        point["source"],
                        str(point.get("created_at") or "timestamp unavailable"),
                    ]
                    for point in passing_points
                ],
                "failed_calls": [point["call"] for point in failed_points],
                "failed_scores": [point["score"] for point in failed_points],
                "failed_details": [
                    [
                        point["iteration"],
                        point["source"],
                        str(point.get("created_at") or "timestamp unavailable"),
                        point.get("failure_class") or "unusable attempt",
                    ]
                    for point in failed_points
                ],
            }
        )

    global_calls: list[int] = [0] if baseline is not None else []
    global_scores: list[float] = [baseline] if baseline is not None else []
    current = baseline
    for evaluation in evaluations:
        score = evaluation["score"]
        if evaluation["eligible"] and (
            current is None or is_better_score(float(score), current, direction)
        ):
            current = score
        if current is not None:
            global_calls.append(int(evaluation["call"]))
            global_scores.append(float(current))

    selected_evaluations = [
        item for item in evaluations if item["selected"] and item["eligible"]
    ]
    selected_point = None
    if selected_evaluations:
        if selected_score is not None:
            selected_evaluation = min(
                selected_evaluations,
                key=lambda item: (
                    abs(float(item["score"]) - selected_score),
                    -int(item["call"]),
                ),
            )
        else:
            selected_evaluation = sorted(
                selected_evaluations,
                key=lambda item: float(item["score"]),
                reverse=direction == "maximize",
            )[0]
        selected_point = {
            "candidate_id": selected_evaluation["candidate_id"],
            "call": selected_evaluation["call"],
            "score": selected_evaluation["score"],
        }

    passing_scores = [
        float(evaluation["score"])
        for evaluation in evaluations
        if evaluation["eligible"]
    ]
    if baseline is not None:
        passing_scores.append(float(baseline))
    passing_count = sum(1 for evaluation in evaluations if evaluation["eligible"])
    return {
        "metric_name": metric_name,
        "metric_direction": direction,
        "baseline": baseline,
        "selected": selected_score,
        "evaluations": len(evaluations),
        "passing_evaluations": passing_count,
        "failed_evaluations": len(evaluations) - passing_count,
        "call_window": _trajectory_call_window(len(evaluations)),
        "score_axis": _trajectory_score_axis(passing_scores),
        "trajectories": trajectories,
        "global_best": {"calls": global_calls, "scores": global_scores},
        "selected_point": selected_point,
    }


def _render_search_trajectory(payload: dict[str, Any]) -> str:
    encoded = escape(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        quote=True,
    )
    evaluations = int(payload.get("evaluations") or 0)
    passing = int(payload.get("passing_evaluations") or 0)
    failed = int(payload.get("failed_evaluations") or 0)
    trajectories = len(payload.get("trajectories") or [])
    axis_type = str((payload.get("score_axis") or {}).get("type") or "linear")
    metric_name = str(payload.get("metric_name") or "score")
    title = str(payload.get("title") or "Complete Search Trajectory")
    unit_label = str(payload.get("unit_label") or "calls")
    group_label = str(payload.get("group_label") or "loops")
    aria_subject = str(payload.get("aria_subject") or "search trajectory")
    aria_label = (
        f"{aria_subject} with {evaluations} {unit_label} across "
        f"{trajectories} {group_label} for {metric_name}."
    )
    return (
        '<div class="trajectory-shell">'
        f'<div class="trajectory-head"><h3>{escape(title)}</h3>'
        f"<span>{evaluations} {escape(unit_label)} / {trajectories} {escape(group_label)} · "
        f"{passing} scored / {failed} failed · "
        f"{axis_type} score axis</span></div>"
        f'<div class="trajectory-plot" role="img" aria-label="{escape(aria_label, quote=True)}" '
        f'data-search-trajectory="{encoded}"></div></div>'
    )


def _render_metric_toolbar(performance: dict[str, Any], default_metric: str) -> str:
    metric_ranges = performance.get("metric_ranges") or {}
    selected_range = metric_ranges.get(default_metric) or {}
    score_baseline = finite_float((performance.get("score") or {}).get("baseline"))
    no_score_baseline = default_metric == "score_gain" and score_baseline is None
    low = (
        "No baseline"
        if no_score_baseline
        else _metric_readout(default_metric, selected_range.get("min"))
    )
    high = (
        "No baseline"
        if no_score_baseline
        else _metric_readout(default_metric, selected_range.get("max"))
    )
    options = (
        ("score-gain", "Score gain"),
        ("score-raw", "Score raw"),
        ("tokens-per-minute", "Tokens/min"),
        ("cost-per-minute", "Cost/min"),
        ("verifier-density", "Verifier/min"),
    )
    buttons = "".join(
        f'<button type="button" data-metric-mode="{key}" aria-pressed="{str(key == default_metric.replace("_", "-")).lower()}">{label}</button>'
        for key, label in options
    )
    return (
        '<div class="metric-lens-toolbar no-print">'
        '<div class="metric-scale" aria-label="Selected metric range">'
        f"<span data-metric-low>{escape(low)}</span>"
        '<span class="metric-scale-bar" aria-hidden="true"><i></i><i></i><i></i><i></i></span>'
        f"<span data-metric-high>{escape(high)}</span>"
        "</div>"
        f'<div class="metric-control" role="group" aria-label="Worker session color metric">{buttons}</div>'
        "</div>"
    )


_SESSION_ALERT_ICON = (
    '<svg class="session-state-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>'
)


def _render_timeline(
    timeline: dict[str, Any],
    *,
    title: str,
    span_label: str = "Observed span",
    include_score_chart: bool = True,
) -> str:
    events = timeline.get("events") or []
    start_epoch = parse_epoch(timeline.get("start_at"))
    duration = timeline.get("duration_seconds")
    if start_epoch is None or not isinstance(duration, int | float):
        return '<div class="panel panel-body">No durable timeline timestamps were observed.</div>'

    main_events = [event for event in events if event.get("lane") == "main"]
    verifier_events = [event for event in events if event.get("lane") == "verifier"]
    worker_events = [event for event in events if event.get("lane") == "worker"]
    performance = timeline.get("performance") or {}
    metric_ranges = performance.get("metric_ranges") or {}
    score_gain_has_baseline = (
        finite_float((performance.get("score") or {}).get("baseline")) is not None
    )
    default_metric = (
        "score_gain"
        if ("score_gain" in metric_ranges or "score_raw" in metric_ranges)
        else (
            "tokens_per_minute"
            if "tokens_per_minute" in metric_ranges
            else next(iter(metric_ranges), "status")
        )
    )
    tracks: list[tuple[str, str, list[dict[str, Any]]]] = [
        ("Main Agent", "main", main_events),
    ]
    for event in worker_events:
        label = str(
            event.get("track_label") or event.get("session_id") or "Worker session"
        )
        tracks.append((label, "worker", [event]))
    if verifier_events:
        tracks.append(("Verifier activity", "parent", verifier_events))
    timeline_width = _timeline_width(float(duration))
    idle_intervals = performance.get("idle_intervals") or []

    rows = []
    worker_track_index = 0
    for label, lane_class, track_events in tracks:
        marks = []
        if lane_class == "worker":
            for idle in idle_intervals:
                left, width = _timeline_position(idle, start_epoch, float(duration))
                idle_label = ""
                if worker_track_index == 0:
                    idle_label = f'<span class="timeline-idle-label">Idle {escape_html(format_duration(idle.get("duration_seconds")))}</span>'
                marks.append(
                    f'<span class="timeline-idle" style="left:{left:.3f}%;width:{width:.3f}%;" '
                    f'title="No active worker sessions for {escape(format_duration(idle.get("duration_seconds")), quote=True)}">'
                    f"{idle_label}</span>"
                )
        for event in track_events:
            left, width = _timeline_position(event, start_epoch, float(duration))
            point = event.get("end_at") is None
            kind = event.get("kind")
            css_class = lane_class
            if kind == "worker_verifier":
                css_class = "worker"
            elif kind == "promotion":
                css_class = "success"
            elif kind != "worker_session" and str(event.get("terminal_state")) in {
                "timed_out",
                "failed",
            }:
                css_class = "failure"
            tooltip = str(event.get("label") or "event")
            if event.get("inferred_end"):
                tooltip += " (end inferred)"
            style = f"left:{left:.3f}%;width:{width:.3f}%;"
            event_attributes = ""
            label_html = "" if point else escape(str(event.get("label") or ""))
            if kind == "worker_session":
                terminal_state = str(event.get("terminal_state") or "unknown")
                failed = terminal_state in {"timed_out", "failed"}
                level = _metric_level(
                    event.get(default_metric), metric_ranges.get(default_metric) or {}
                )
                css_class = "worker worker-session"
                if level is not None:
                    css_class += f" metric-level-{level}"
                if failed:
                    css_class += " session-failure"
                metric_attributes = []
                for metric_name in (
                    "score_gain",
                    "score_raw",
                    "tokens_per_minute",
                    "cost_per_minute",
                    "verifier_density",
                ):
                    metric_value = finite_float(event.get(metric_name))
                    if metric_value is not None:
                        metric_attributes.append(
                            f'data-metric-{metric_name.replace("_", "-")}="{metric_value:.9f}"'
                        )
                event_attributes = (
                    f'data-terminal-state="{escape(terminal_state, quote=True)}" '
                    + " ".join(metric_attributes)
                    + " "
                )
                metric_value = event.get(default_metric)
                default_readout = (
                    "No baseline"
                    if default_metric == "score_gain" and not score_gain_has_baseline
                    else _metric_readout(default_metric, metric_value)
                )
                score_gain_readout = (
                    "No baseline"
                    if not score_gain_has_baseline
                    else _metric_readout("score_gain", event.get("score_gain"))
                )
                label_html = (
                    _SESSION_ALERT_ICON if failed else ""
                ) + f'<span class="metric-readout">{escape(default_readout)}</span>'
                details = [
                    f"candidate {event.get('candidate_id')}",
                    f"duration {format_duration(event.get('duration_seconds'))}",
                    f"score gain {score_gain_readout}",
                    f"score raw {_metric_readout('score_raw', event.get('score_raw'))}",
                    f"tokens/min {_metric_readout('tokens_per_minute', event.get('tokens_per_minute'))}",
                    f"cost/min {_metric_readout('cost_per_minute', event.get('cost_per_minute'))}",
                    f"verifier density {_metric_readout('verifier_density', event.get('verifier_density'))}",
                ]
                if finite_float(event.get("score")) is not None:
                    details.append(f"score {numeric_value(event.get('score'), digits=4)}")
                tooltip += " | " + " | ".join(details)
            marks.append(
                f'<span class="timeline-event {css_class}{" point" if point else ""}" {event_attributes}'
                f'style="{style}" title="{escape(tooltip, quote=True)}">{label_html}</span>'
            )
        label_html = escape(label)
        row_class = "timeline-row"
        if lane_class == "worker" and track_events:
            event = track_events[0]
            session_id = str(event.get("session_id") or label)
            suffix = session_id.rsplit("_", 1)[-1]
            candidate_id = str(event.get("candidate_id") or "unknown")
            attempt_index = int(event.get("attempt_index") or 1)
            attempt_count = int(event.get("attempt_count") or 1)
            score = finite_float(event.get("score"))
            retry = (
                f'<span class="retry-badge">retry {attempt_index}/{attempt_count}</span>'
                if attempt_count > 1
                else ""
            )
            score_text = (
                f" / score {numeric_value(score, digits=4)}" if score is not None else ""
            )
            label_html = (
                f'<strong title="{escape(session_id, quote=True)}">agent_{escape(suffix)}{retry}</strong>'
                f"<small>{escape(candidate_id)}{escape(score_text)}</small>"
            )
            if attempt_count > 1:
                row_class += " redispatched"
            worker_track_index += 1
        rows.append(
            f'<div class="{row_class}">'
            f'<div class="timeline-label">{label_html}</div>'
            f'<div class="timeline-track">{"".join(marks)}</div>'
            "</div>"
        )
    end_epoch = start_epoch + float(duration)
    event_items = []
    for event in events:
        event_items.append(
            "<li>"
            f'<time class="timeline-time">{escape_html(event.get("start_at"))}</time>'
            f'<span class="lane">{escape_html(event.get("lane"))}</span>'
            f"<span>{escape_html(event.get('label'))}"
            f"{' <em>(end inferred)</em>' if event.get('inferred_end') else ''}</span>"
            "</li>"
        )
    metric_lens = bool(worker_events and metric_ranges)
    score_chart = (
        _render_score_chart(performance, start_epoch, float(duration))
        if worker_events and include_score_chart
        else ""
    )
    toolbar = _render_metric_toolbar(performance, default_metric) if metric_lens else ""
    default_mode = default_metric.replace("_", "-") if metric_lens else "status"
    return (
        f'<div class="panel timeline-shell" data-metric-mode="{default_mode}"'
        f' data-score-gain-baseline="{str(score_gain_has_baseline).lower()}"'
        f"{' data-metric-lens' if metric_lens else ''}>"
        '<div class="timeline-head">'
        f"<h2>{escape(title)}</h2>"
        f'<span class="mono">{escape(span_label)}: {escape(format_duration(duration))}</span>'
        "</div>"
        f"{toolbar}"
        f'<div class="timeline-scroll" tabindex="0" aria-label="{escape(title, quote=True)} scroll area">'
        f'<div class="timeline" style="--timeline-width:{timeline_width}px">'
        f"{score_chart}"
        f'<div class="timeline-rows" data-track-count="{len(tracks)}">{"".join(rows)}</div>'
        '<div class="timeline-axis">'
        f"<span>{escape(format_timestamp(start_epoch) or '')}</span>"
        f"<span>+{escape(format_duration(float(duration) / 2))}</span>"
        f"<span>{escape(format_timestamp(end_epoch) or '')}</span>"
        "</div></div></div>"
        '<div class="timeline-key">'
        '<span><i class="key-dot"></i>Main agent</span>'
        '<span><i class="key-dot worker"></i>Worker session / worker verifier</span>'
        '<span><i class="key-dot parent"></i>Parent verifier</span>'
        f"{'<span>Fill intensity = selected metric</span><span>Red outline = timed out / failed</span><span>Retry n/N = same candidate redispatch</span>' if metric_lens else ''}"
        "</div></div>"
        '<details class="summary-block event-log"><summary>Chronological event evidence</summary>'
        f'<div><ul class="event-list">{"".join(event_items)}</ul></div></details>'
    )


def _render_shared_evidence_view(task: dict[str, Any]) -> str:
    verifier_enabled = "verifier_tasks" in task
    local_rows = [
        {
            **iteration,
            "candidate_id": candidate.get("candidate_id"),
            "candidate_selected": bool(candidate.get("selected")),
            "view_state": reported_view_state(iteration),
        }
        for candidate in task.get("candidates") or []
        for iteration in candidate.get("iterations") or []
        if iteration.get("agent_session_id")
    ]
    if not local_rows:
        return "<p>No worker Evidence was persisted for this Search task.</p>"

    official_rows = []
    for item in local_rows:
        for evaluation in item.get("external_evaluations") or []:
            if not isinstance(evaluation, dict):
                continue
            is_official = (
                evaluation.get("authority") == "edgebench_official_hidden_judge"
                or evaluation.get("source") == "edgebench"
            )
            score_100 = finite_float(evaluation.get("score_0_100"))
            valid = evaluation.get("valid")
            evidence_source = "official" if is_official else "external"
            official_rows.append(
                {
                    **item,
                    "created_at": evaluation.get("published_at")
                    or item.get("created_at"),
                    "score": (
                        score_100 if score_100 is not None else evaluation.get("score")
                    ),
                    "score_kind": (
                        f"{evidence_source} / 100"
                        if score_100 is not None
                        else f"{evidence_source} raw score"
                    ),
                    "disposition": (
                        "valid"
                        if valid is True
                        else "invalid"
                        if valid is False
                        else evaluation.get("status") or evidence_source
                    ),
                    "hypothesis": (
                        "Official Judge evaluation"
                        if is_official
                        else "External evaluation"
                    ),
                    "view": evaluation.get("summary")
                    or evaluation.get("error")
                    or f"{evidence_source.title()} evaluation {evaluation.get('status') or 'recorded'}.",
                    "view_state": evidence_source,
                    "view_error": None,
                    "annotation_monitor": None,
                    "evidence_source": evidence_source,
                }
            )

    rows_payload = [*local_rows, *official_rows]
    rows_payload.sort(
        key=lambda item: (
            parse_epoch(item.get("created_at")) or float("inf"),
            str(item.get("candidate_id") or ""),
            int(item.get("iteration") or 0),
        )
    )
    candidates = sorted(
        {
            str(item.get("candidate_id"))
            for item in rows_payload
            if item.get("candidate_id")
        }
    )
    dispositions = sorted(
        {str(item.get("disposition") or "unsettled") for item in rows_payload}
    )
    view_states = sorted(
        {str(item.get("view_state") or "not_requested") for item in rows_payload}
    )
    published = sum(bool(item.get("view")) for item in local_rows)
    state_counts = Counter(
        str(item.get("view_state") or "not_requested") for item in local_rows
    )

    def options(values: list[str]) -> str:
        return "".join(
            f'<option value="{escape(value, quote=True)}">{escape(value.replace("_", " "))}</option>'
            for value in values
        )

    def published_tool_views(item: dict[str, Any]) -> str:
        if item.get("evidence_source"):
            return '<div class="evidence-view-empty">Not applicable</div>'
        if item.get("view_state") == "closeout_exempt":
            return '<div class="evidence-view-empty">Closeout-exempt</div>'
        views = item.get("published_tool_views")
        if not isinstance(views, list) or not views:
            return '<div class="evidence-view-empty">None published</div>'
        rendered = []
        for tool_view in views:
            if not isinstance(tool_view, dict):
                continue
            tool_id = tool_view.get("tool_id")
            summary = tool_view.get("summary")
            if not isinstance(tool_id, str) or not isinstance(summary, str):
                continue
            detail = []
            entrypoint = tool_view.get("entrypoint")
            if isinstance(entrypoint, str) and entrypoint:
                detail.append(f"Entrypoint: {entrypoint}")
            when_to_use = tool_view.get("when_to_use")
            if isinstance(when_to_use, str) and when_to_use:
                detail.append(f"Use: {when_to_use}")
            rendered.append(
                '<div class="evidence-tool">'
                f'<div class="evidence-tool-id">{escape_html(tool_id)}</div>'
                f"<div>{escape_html(summary)}</div>"
                + (
                    f'<div class="evidence-tool-detail">{escape_html(" | ".join(detail))}</div>'
                    if detail
                    else ""
                )
                + "</div>"
            )
        return (
            f'<div class="evidence-tool-list">{"".join(rendered)}</div>'
            if rendered
            else '<div class="evidence-view-empty">None published</div>'
        )

    def tool_adoption_summary(item: dict[str, Any]) -> str:
        if item.get("evidence_source"):
            return '<div class="evidence-view-empty">Not applicable</div>'
        adoptions = item.get("adopted_tools")
        if not isinstance(adoptions, list) or not adoptions:
            return '<div class="evidence-view-empty">No shared tool adopted</div>'
        rendered = []
        for adoption in adoptions:
            if not isinstance(adoption, dict):
                continue
            tool_id = adoption.get("tool_id")
            snapshot_hash = adoption.get("snapshot_hash")
            if not isinstance(tool_id, str) or not isinstance(snapshot_hash, str):
                continue
            receipt_id = adoption.get("receipt_id")
            detail = f"snapshot {snapshot_hash[:12]}"
            if isinstance(receipt_id, str) and receipt_id:
                detail += f" | receipt {receipt_id}"
            rendered.append(
                '<div class="evidence-tool">'
                f'<div class="evidence-tool-id">{escape_html(tool_id)}</div>'
                f'<div class="evidence-tool-detail">{escape_html(detail)}</div>'
                "</div>"
            )
        if not rendered:
            return '<div class="evidence-view-empty">No shared tool adopted</div>'
        qualifier = (
            '<div class="evidence-tool-detail">Confounded adoption trial</div>'
            if item.get("adoption_confounded") is True
            else '<div class="evidence-tool-detail">Isolated adoption trial</div>'
        )
        return f'<div class="evidence-tool-list">{"".join(rendered)}{qualifier}</div>'

    def toolization_review(item: dict[str, Any]) -> str:
        if item.get("evidence_source"):
            return '<div class="evidence-view-empty">Not applicable</div>'
        if item.get("view_state") == "closeout_exempt":
            return (
                '<div class="evidence-tool">'
                '<div class="evidence-view-empty">Closeout-exempt</div>'
                '<div class="evidence-tool-detail">Decision evidence preserved; '
                "supplemental review not required</div></div>"
            )
        decision = item.get("toolization_decision")
        advisories = item.get("toolization_advisories") or []
        staged_entries = item.get("shared_tool_staged_entries") or []
        publish_status = item.get("shared_tool_publish_status")
        if not isinstance(decision, dict):
            decision_copy = '<div class="evidence-view-empty">Review missing</div>'
        else:
            outcome = decision.get("outcome")
            signals = decision.get("signals") or []
            exclusion = decision.get("exclusion")
            tool_names = decision.get("tool_names") or []
            details = []
            if signals:
                details.append("Signals: " + ", ".join(str(item) for item in signals))
            if exclusion:
                details.append(f"Exclusion: {exclusion}")
            if tool_names:
                details.append("Tools: " + ", ".join(str(item) for item in tool_names))
            decision_copy = (
                f'<div class="evidence-tool-id">{escape_html(outcome)}</div>'
                f"<div>{escape_html(decision.get('rationale'))}</div>"
                + (
                    f'<div class="evidence-tool-detail">{escape_html(" | ".join(details))}</div>'
                    if details
                    else ""
                )
            )
        settlement = [
            f"Staged: {', '.join(str(item) for item in staged_entries) or 'none'}",
            f"Publish: {publish_status or 'unknown'}",
        ]
        if advisories:
            settlement.append(
                "Advisory: " + ", ".join(str(item) for item in advisories)
            )
        return (
            '<div class="evidence-tool">'
            f"{decision_copy}"
            f'<div class="evidence-tool-detail">{escape_html(" | ".join(settlement))}</div>'
            "</div>"
        )

    rows = []
    for item in rows_payload:
        candidate_id = str(item.get("candidate_id") or "")
        disposition = str(item.get("disposition") or "unsettled")
        view_state = str(item.get("view_state") or "not_requested")
        commit = str(item.get("git_head") or "")
        evidence_source = item.get("evidence_source")
        view = item.get("view")
        tool_view_copy = published_tool_views(item)
        toolization_copy = toolization_review(item)
        adoption_copy = tool_adoption_summary(item)
        closeout_exempt = view_state == "closeout_exempt"
        view_copy = (
            f'<div class="evidence-view-copy">{escape_html(view)}</div>'
            if view
            else (
                '<div class="evidence-view-empty">Decision evidence covered</div>'
                if closeout_exempt
                else '<div class="evidence-view-empty">Not published</div>'
            )
        )
        view_error = (
            '<div class="evidence-view-monitor">Supplemental View omitted by '
            "normal platform-stop closeout.</div>"
            if closeout_exempt
            else f'<div class="evidence-view-error">{escape_html(item.get("view_error"))}</div>'
            if item.get("view_error")
            else ""
        )
        monitor = item.get("annotation_monitor")
        monitor_copy = ""
        if isinstance(monitor, dict):
            last_events = monitor.get("last_events")
            last_event = (
                last_events[-1].get("type")
                if isinstance(last_events, list)
                and last_events
                and isinstance(last_events[-1], dict)
                else None
            )
            monitor_parts = [
                f"Annotator {monitor.get('state') or 'unknown'}",
                format_duration(finite_float(monitor.get("elapsed_seconds"))),
                f"{numeric_value(monitor.get('json_lines'))} JSON events",
                f"last {last_event}" if last_event else None,
                (
                    f"stdout {numeric_value(monitor.get('stdout_bytes'))} B"
                    if monitor.get("stdout_bytes") is not None
                    else None
                ),
            ]
            monitor_copy = (
                '<div class="evidence-view-monitor">'
                + escape_html(" | ".join(str(part) for part in monitor_parts if part))
                + "</div>"
            )
        attempt = item.get("hypothesis") or item.get("summary")
        row_classes = []
        if item.get("candidate_selected"):
            row_classes.append("selected-row")
        if evidence_source:
            row_classes.append("official-evidence-row")
        score = numeric_value(item.get("score"), digits=3 if evidence_source else 2)
        score_copy = escape_html(score)
        if item.get("score_kind"):
            score_copy = (
                f"<strong>{escape_html(score)}</strong>"
                f'<div class="evidence-score-kind">{escape_html(item.get("score_kind"))}</div>'
            )
        verifier_record = item.get("verifier_record")
        verifier_copy = "Not observed"
        if isinstance(verifier_record, dict):
            verifier_copy = (
                f"quality {numeric_value(verifier_record.get('quality_score'), digits=3)}"
            )
            scores = verifier_record.get("scores")
            if isinstance(scores, dict):
                verifier_copy += "".join(
                    f" | {name} {numeric_value(value, digits=3)}"
                    for name, value in sorted(scores.items())
                )
        elif isinstance(item.get("verifier_task"), dict):
            verifier_copy = (
                f"Verifier {item['verifier_task'].get('state') or 'unknown'}"
            )
        verifier_cell = (
            f'<td class="mono">{escape_html(verifier_copy)}</td>' if verifier_enabled else ""
        )
        rows.append(
            f'<tr class="{escape(" ".join(row_classes), quote=True)}"'
            " data-evidence-row"
            f' data-candidate="{escape(candidate_id, quote=True)}"'
            f' data-disposition="{escape(disposition, quote=True)}"'
            f' data-view-state="{escape(view_state, quote=True)}">'
            f'<td class="mono">{escape_html(item.get("created_at"))}</td>'
            f'<td class="mono"><strong>{escape_html(candidate_id)}</strong></td>'
            f'<td class="mono">{escape_html(item.get("iteration"))}</td>'
            f'<td class="mono">{score_copy}</td>'
            f"{verifier_cell}"
            f"<td>{render_status(disposition)}</td>"
            f'<td><div class="evidence-copy">{escape_html(attempt)}</div></td>'
            f'<td>{view_copy}{view_error}{monitor_copy}<div class="evidence-view-meta">{render_status(view_state)}</div></td>'
            f"<td>{toolization_copy}</td>"
            f"<td>{tool_view_copy}</td>"
            f"<td>{adoption_copy}</td>"
            f'<td><code class="revision" title="{escape(commit, quote=True)}">'
            f"{escape_html(commit[:12] if commit else None)}</code></td>"
            "</tr>"
        )

    return (
        "<div data-evidence-view>"
        '<div class="evidence-view-toolbar no-print">'
        '<div class="evidence-view-summary">'
        f"<span><strong>{escape_html(len(local_rows))}/{escape_html(len(local_rows))}</strong>Decision evidence covered</span>"
        f"<span><strong>{escape_html(published)}</strong>Views published</span>"
        f"<span><strong>{escape_html(len(official_rows))}</strong>Official evaluations</span>"
        f"<span><strong>{escape_html(state_counts.get('pending', 0))}</strong>Pending</span>"
        f"<span><strong>{escape_html(state_counts.get('closeout_exempt', 0))}</strong>Closeout-exempt</span>"
        f"<span><strong>{escape_html(state_counts.get('terminal_error', 0))}</strong>Annotation errors</span>"
        "</div>"
        '<div class="evidence-view-filters">'
        '<label class="evidence-view-filter"><span>Candidate</span>'
        f'<select data-evidence-filter="candidate"><option value="">All</option>{options(candidates)}</select></label>'
        '<label class="evidence-view-filter"><span>Settlement</span>'
        f'<select data-evidence-filter="disposition"><option value="">All</option>{options(dispositions)}</select></label>'
        '<label class="evidence-view-filter"><span>View state</span>'
        f'<select data-evidence-filter="view-state"><option value="">All</option>{options(view_states)}</select></label>'
        '<label class="evidence-view-filter"><span>Text</span>'
        '<input type="search" data-evidence-filter="text" aria-label="Filter Evidence text"></label>'
        f'<div class="evidence-view-count" data-evidence-count aria-live="polite">{len(rows_payload)} of {len(rows_payload)} rows</div>'
        "</div></div>"
        '<div class="table-scroll evidence-view-scroll">'
        '<table class="evidence-view-table"><thead><tr>'
        "<th>Time</th><th>Candidate</th><th>Iteration</th><th>Score</th>"
        f"{'<th>Quality / adaptive metrics</th>' if verifier_enabled else ''}"
        "<th>Settlement</th><th>Worker attempt</th><th>Objective View</th>"
        "<th>Toolization Review</th><th>Published Tool View</th>"
        "<th>Tool Adoption Summary</th><th>Revision</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        '<p class="footnote">Decision evidence coverage counts durable verifier-backed iteration settlement. '
        "Worker attempt is the candidate-authored settled hypothesis. Objective View is the immutable "
        "best-effort supplemental annotation bound to the exact candidate, "
        "iteration, and revision; missing Views are not inferred from worker text. Published "
        "Toolization Review compares the worker decision with the authoritative staging and "
        "publication facts; its advisories do not affect scoring or settlement. Published "
        "Tool Views appear only after that annotation binds them to the published snapshot. Tool "
        "adoption summarizes verifier-consumed copy receipts and whether the trial was confounded. Official "
        "evaluation rows are external Judge observations bound to that same exact identity; "
        "they do not change local settlement or ranking.</p>"
        "</div>"
    )


def _render_candidates(task: dict[str, Any]) -> str:
    candidates = task.get("candidates") or []
    if not candidates:
        return "<p>No candidates were persisted.</p>"
    verifier_enabled = "verifier_tasks" in task
    rows = []
    for candidate in candidates:
        current_value = candidate.get("latest_verifier_record") or {}
        verifier_cells = ""
        if verifier_enabled:
            verifier_cells = (
                f"<td>{render_status(candidate.get('allocation_eligibility') or 'eligible')}</td>"
                f'<td class="mono">{escape_html(candidate.get("allocation_depth"))}</td>'
                f'<td class="mono">{escape_html((candidate.get("expansion_source") or {}).get("candidate_id"))}</td>'
                f'<td class="mono">{escape_html(numeric_value(current_value.get("quality_score")))}</td>'
            )
        rows.append(
            f'<tr class="{"selected-row" if candidate.get("selected") else ""}">'
            f'<td class="mono"><strong>{escape_html(candidate.get("candidate_id"))}</strong></td>'
            f"<td>{render_status('selected' if candidate.get('selected') else candidate.get('status'))}</td>"
            f"{verifier_cells}"
            f'<td class="mono">{escape_html(numeric_value(candidate.get("score")))}</td>'
            f'<td class="mono">{escape_html(numeric_value(candidate.get("best_score")))}</td>'
            f"<td>{escape_html(candidate.get('process_passed'))}</td>"
            f'<td class="mono">{escape_html(candidate.get("iterations_total"))}</td>'
            f'<td class="mono">{escape_html(", ".join(candidate.get("session_ids") or []) or None)}</td>'
            f"<td>{escape_html(', '.join(candidate.get('changed_files') or []) or None)}</td>"
            f"<td>{escape_html(candidate.get('hypothesis'))}</td>"
            "</tr>"
        )
    verifier_headers = ""
    if verifier_enabled:
        verifier_headers = (
            "<th>Allocation</th><th>Depth</th><th>Source</th><th>Latest quality</th>"
        )
    return (
        '<div class="table-scroll"><table><thead><tr>'
        f"<th>Candidate</th><th>Status</th>{verifier_headers}"
        "<th>Final score</th><th>Best score</th><th>Process pass</th>"
        "<th>Iterations</th><th>Sessions</th><th>Changed files</th><th>Hypothesis</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _render_sessions(task: dict[str, Any]) -> str:
    sessions = task.get("sessions") or []
    if not sessions:
        return "<p>No worker sessions were persisted.</p>"
    rows = []
    for session in sessions:
        terminal = session.get("terminal_state") or (
            "timed_out" if session.get("timed_out") else "unknown"
        )
        dispatch_label = "{} / {}".format(
            session.get("dispatch_index"), session.get("dispatch_count")
        )
        rows.append(
            "<tr>"
            f'<td class="mono"><strong>{escape_html(session.get("agent_session_id"))}</strong></td>'
            f'<td class="mono">{escape_html(dispatch_label)}</td>'
            f'<td class="mono">{escape_html(session.get("candidate_id"))}</td>'
            f"<td>{escape_html(session.get('agent_harness'))}</td>"
            f"<td>{escape_html(session.get('runtime_provider'))}</td>"
            f"<td>{escape_html(session.get('provider'))}</td>"
            f"<td>{escape_html(session.get('model'))}</td>"
            f"<td>{render_status(terminal)}</td>"
            f'<td class="mono">{escape_html(format_duration(session.get("duration_seconds")))}</td>'
            f'<td class="mono">{escape_html(numeric_value(session.get("processed_tokens")))}</td>'
            f'<td class="mono">{escape_html(format_cost(session.get("cost_usd")))}</td>'
            f'<td class="mono">{escape_html(session.get("verifier_runs"))}</td>'
            f'<td class="mono">{escape_html(session.get("global_evidence_reads_explicit"))}</td>'
            f'<td class="mono">{escape_html(session.get("global_evidence_reads_verifier_injected"))}</td>'
            f'<td class="mono">{escape_html(session.get("evidence_detail_reads_total"))}</td>'
            f'<td class="mono">{escape_html(session.get("latest_global_evidence_read_at"))}</td>'
            f'<td class="mono">{escape_html(session.get("latest_evidence_detail_read_at"))}</td>'
            "</tr>"
        )
    return (
        '<div class="table-scroll"><table><thead><tr>'
        "<th>Session</th><th>Dispatch</th><th>Candidate</th><th>Harness</th><th>Runtime provider</th><th>Model provider</th><th>Model</th>"
        "<th>Terminal state</th><th>Duration</th><th>Processed tokens</th><th>Estimated cost</th><th>Verifier runs</th>"
        "<th>Evidence reads</th><th>Injected evidence</th><th>Detail reads</th><th>Last evidence read</th><th>Last detail read</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _render_statistics(task: dict[str, Any]) -> str:
    statistics = task.get("statistics")
    if not isinstance(statistics, dict):
        return "<p>Unified statistics were not available for this Search task.</p>"
    timing = statistics.get("timing") or {}
    workers = statistics.get("workers") or {}
    verifiers = statistics.get("verifiers") or {}
    usage = statistics.get("usage") or {}
    efficiency = statistics.get("efficiency") or {}
    lineage = statistics.get("lineage") or {}
    workspace_operations = statistics.get("workspace_operations") or {}
    workspace_capacity = workspace_operations.get("capacity_estimate") or {}
    tables = [
        ("Timing", timing, {key: format_duration for key in timing if "_seconds" in key}),
        (
            "Workspace Capacity Preflight",
            {
                key: workspace_capacity.get(key)
                for key in (
                    "basis",
                    "worktree_nodes",
                    "source_scope_bytes",
                    "materialized_source_bytes",
                    "checkout_bytes_per_node",
                    "candidate_checkouts_bytes",
                    "shared_object_store_bytes",
                    "estimated_incremental_bytes",
                    "conservative_upper_bound_bytes",
                    "available_bytes",
                    "fits_available_space",
                )
            },
            {key: format_bytes for key in workspace_capacity if key.endswith("_bytes")},
        ),
        (
            "Workers",
            workers,
            {key: format_percent for key in workers if key.endswith("_rate")},
        ),
        (
            "Verifiers",
            verifiers,
            {key: format_percent for key in verifiers if key.endswith("_rate")},
        ),
        (
            "Usage",
            {
                key: usage.get(key)
                for key in (
                    "processed_tokens",
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "cost_usd",
                    "tool_calls",
                )
            },
            {"cost_usd": format_cost},
        ),
        (
            "Efficiency",
            efficiency,
            {key: format_cost for key in efficiency if key.endswith("_usd")},
        ),
        ("Lineage", lineage, {}),
    ]
    return (
        '<div class="stats-grid">'
        + "".join(
            '<div class="stats-table">'
            f"<h3>{escape(title)}</h3>{_stat_rows(values, formatters)}"
            "</div>"
            for title, values, formatters in tables
        )
        + "</div>"
    )


def _render_stop_hook_statistics(statistics: dict[str, Any]) -> str:
    by_event = statistics.get("by_event") or {}
    source_available = statistics.get("source_available") is True

    def event_summary(event_name: str) -> dict[str, Any]:
        if not source_available:
            return {
                "calls": None,
                "block": None,
                "allow": None,
                "skipped": None,
                "error": None,
                "unknown": None,
                "hook_time": None,
            }
        summary = by_event.get(event_name) or {}
        decisions = summary.get("decisions") or {}
        return {
            "calls": summary.get("events_total", 0),
            "block": decisions.get("block", 0),
            "allow": decisions.get("allow", 0),
            "skipped": decisions.get("skipped", 0),
            "error": decisions.get("error", 0),
            "unknown": decisions.get("unknown", 0),
            "hook_time": summary.get("duration_ms_total", 0.0),
        }

    overview = {
        "calls": statistics.get("events_total", 0) if source_available else None,
        "hook_time": (
            statistics.get("duration_ms_total", 0.0) if source_available else None
        ),
        "captured_through": statistics.get("captured_through"),
    }
    summary_tables = (
        '<div class="stats-grid">'
        '<div class="stats-table"><h3>All Stop Hooks</h3>'
        f"{_stat_rows(overview, {'hook_time': format_milliseconds})}</div>"
        '<div class="stats-table"><h3>Top-level Stop</h3>'
        f"{_stat_rows(event_summary('Stop'), {'hook_time': format_milliseconds})}</div>"
        '<div class="stats-table"><h3>SubagentStop</h3>'
        f"{_stat_rows(event_summary('SubagentStop'), {'hook_time': format_milliseconds})}</div>"
        "</div>"
    )

    subagent_rows = []
    for subagent in statistics.get("subagents") or []:
        decisions = subagent.get("decisions") or {}
        subagent_rows.append(
            "<tr>"
            f'<td class="mono"><strong>{escape_html(subagent.get("identity"))}</strong></td>'
            f"<td>{escape_html(subagent.get('identity_source'))}</td>"
            f'<td class="mono">{escape_html(", ".join(subagent.get("host_agent_ids") or []) or None)}</td>'
            f'<td class="mono">{escape_html(", ".join(subagent.get("run_ids") or []) or None)}</td>'
            f'<td class="mono">{escape_html(", ".join(subagent.get("candidate_ids") or []) or None)}</td>'
            f'<td class="mono">{escape_html(subagent.get("events_total"))}</td>'
            f'<td class="mono">{escape_html(decisions.get("block", 0))}</td>'
            f'<td class="mono">{escape_html(decisions.get("allow", 0))}</td>'
            f'<td class="mono">{escape_html(decisions.get("skipped", 0))}</td>'
            f'<td class="mono">{escape_html(decisions.get("error", 0))}</td>'
            f'<td class="mono">{escape_html(format_milliseconds(subagent.get("duration_ms_total")))}</td>'
            f'<td class="mono">{escape_html(subagent.get("last_event_at"))}</td>'
            "</tr>"
        )
    if not source_available:
        subagent_table = (
            "<p>Stop-hook event evidence was not persisted for this report. "
            "Counts cannot be distinguished from zero.</p>"
        )
    elif subagent_rows:
        subagent_table = (
            '<div class="table-scroll"><table class="hook-table"><thead><tr>'
            "<th>Subagent</th><th>Identity source</th><th>Host agent</th><th>Run</th><th>Candidate</th>"
            "<th>Calls</th><th>Block</th><th>Allow</th><th>Skipped</th><th>Error</th>"
            "<th>Hook time</th><th>Last event</th>"
            f"</tr></thead><tbody>{''.join(subagent_rows)}</tbody></table></div>"
        )
    else:
        subagent_table = (
            "<p>No SubagentStop invocation was captured in this report snapshot.</p>"
        )

    event_rows = []
    for event in statistics.get("events") or []:
        runtime_agent = event.get("agent_session_id")
        host_agent = event.get("host_agent_id")
        reason = event.get("error") or event.get("reason")
        event_rows.append(
            "<tr>"
            f'<td class="mono">{escape_html(event.get("started_at"))}</td>'
            f"<td>{escape_html(event.get('hook_event_name'))}</td>"
            f"<td>{render_status(event.get('decision'))}</td>"
            f'<td class="mono">{escape_html(runtime_agent)}</td>'
            f'<td class="mono">{escape_html(host_agent)}</td>'
            f'<td class="mono">{escape_html(event.get("run_id"))}</td>'
            f'<td class="mono">{escape_html(event.get("candidate_id"))}</td>'
            f'<td class="mono">{escape_html(format_milliseconds(event.get("duration_ms")))}</td>'
            f"<td>{escape_html(event.get('stop_reason'))}</td>"
            f"<td>{escape_html(reason)}</td>"
            "</tr>"
        )
    event_evidence = (
        '<details class="summary-block"><summary>Per-invocation hook evidence '
        f'({len(event_rows)})</summary><div><div class="table-scroll"><table class="hook-table"><thead><tr>'
        "<th>Started</th><th>Event</th><th>Decision</th><th>Agent session</th>"
        "<th>Host agent</th><th>Run</th><th>Candidate</th><th>Hook time</th>"
        "<th>Stop reason</th><th>Gate reason / error</th>"
        f"</tr></thead><tbody>{''.join(event_rows)}</tbody></table></div></div></details>"
        if event_rows
        else ""
    )
    return (
        summary_tables
        + '<div class="subsection"><h3>Subagent Breakdown</h3>'
        + subagent_table
        + "</div>"
        + event_evidence
    )


def _render_loop_agent_statistics(statistics: dict[str, Any]) -> str:
    rows = statistics.get("rows") or []
    if not rows:
        return "<p>No Codex loop-agent session was persisted for this report.</p>"

    def observed(row: dict[str, Any], value: Any) -> Any:
        return value if row.get("activity_available") else None

    def count_pair(total: Any, tail: Any, *, label: str = "tail") -> str | None:
        if total is None or tail is None:
            return None
        return f"{numeric_value(total)} total / {numeric_value(tail)} {label}"

    def context_label(row: dict[str, Any]) -> str | None:
        first = finite_float(row.get("context_percent_at_first_block_nearest"))
        final = finite_float(row.get("context_percent_final"))
        maximum = finite_float(row.get("context_percent_max"))
        if first is None and final is None and maximum is None:
            return None

        def percent(value: float | None) -> str:
            return (
                f"{numeric_value(value, digits=1)}%" if value is not None else "Not observed"
            )

        return f"{format_percent(first)} → {format_percent(final)} (max {format_percent(maximum)})"

    summary_rows = []
    for row in rows:
        activity = row.get("activity") or {}
        post_last = row.get("post_last_verifier") or {}
        hook_stream_available = row.get("hook_stream_available") is True
        stop_label = (
            f"{numeric_value(row.get('blocked_stops'))} block / "
            f"{numeric_value(row.get('allowed_stops'))} allow"
            if hook_stream_available
            else None
        )
        productive_label = (
            f"{numeric_value(row.get('productive_blocked_stops'))} / "
            f"{numeric_value(row.get('blocked_stops'))}"
            if hook_stream_available
            else None
        )
        verifier_label = (
            f"{numeric_value(row.get('verified_revision_changes_after_blocked_stops'))} revisions / "
            f"{numeric_value(row.get('verifier_runs_after_blocked_stops'))} verifiers / "
            f"{numeric_value(row.get('improvements_after_blocked_stops'))} improvements"
            if hook_stream_available
            else None
        )
        message_label = observed(
            row,
            (
                f"{numeric_value(post_last.get('assistant_messages'))} messages / "
                f"{numeric_value(post_last.get('empty_messages'))} empty / "
                f"{numeric_value(post_last.get('best_remains_messages'))} best-remains / "
                f"{numeric_value(post_last.get('cannot_continue_messages'))} cannot-continue"
            ),
        )
        summary_rows.append(
            "<tr>"
            f'<td class="mono"><strong>{escape_html(row.get("candidate_id"))}</strong></td>'
            f'<td class="mono">{escape_html(row.get("agent_session_id"))}</td>'
            f"<td>{render_status('incumbent' if row.get('selected') else 'follower')}</td>"
            f'<td class="mono">{escape_html(numeric_value(row.get("best_score")))}</td>'
            f'<td class="mono">{escape_html(stop_label)}</td>'
            f'<td class="mono">{escape_html(productive_label)}</td>'
            f'<td class="mono">{escape_html(verifier_label)}</td>'
            f'<td class="mono">{escape_html(observed(row, count_pair(activity.get("context_calls"), post_last.get("context_calls"))))}</td>'
            f'<td class="mono">{escape_html(observed(row, count_pair(activity.get("global_evidence_calls"), post_last.get("global_evidence_calls"))))}</td>'
            f'<td class="mono">{escape_html(message_label)}</td>'
            f'<td class="mono">{escape_html(context_label(row) if row.get("activity_available") else None)}</td>'
            f"<td>{render_status(row.get('tail_signal'))}</td>"
            "</tr>"
        )
    summary_table = (
        '<div class="table-scroll"><table class="hook-table"><thead><tr>'
        "<th>Candidate</th><th>Loop agent</th><th>Final role</th><th>Best score</th>"
        "<th>SubagentStop</th><th>Productive blocks</th><th>Post-block verified work</th>"
        "<th>Context reads</th><th>Global-evidence reads</th><th>After last verifier</th>"
        "<th>Context near first block → final</th><th>Tail signal</th>"
        f"</tr></thead><tbody>{''.join(summary_rows)}</tbody></table></div>"
    )

    window_rows = []
    for window in statistics.get("stop_windows") or []:
        window_rows.append(
            "<tr>"
            f'<td class="mono">{escape_html(window.get("candidate_id"))}</td>'
            f'<td class="mono">{escape_html(window.get("agent_session_id"))}</td>'
            f'<td class="mono">{escape_html(window.get("stop_index"))}</td>'
            f"<td>{render_status(window.get('decision'))}</td>"
            f'<td class="mono">{escape_html(window.get("started_at"))}</td>'
            f'<td class="mono">{escape_html(window.get("context_calls"))}</td>'
            f'<td class="mono">{escape_html(window.get("global_evidence_calls"))}</td>'
            f'<td class="mono">{escape_html(window.get("edit_capable_calls"))}</td>'
            f'<td class="mono">{escape_html(window.get("verifier_runs"))}</td>'
            f'<td class="mono">{escape_html(window.get("verified_revision_changes"))}</td>'
            f'<td class="mono">{escape_html(window.get("improvements"))}</td>'
            f'<td class="mono">{escape_html(window.get("assistant_messages"))}</td>'
            f'<td class="mono">{escape_html(window.get("empty_messages"))}</td>'
            f'<td class="mono">{escape_html(window.get("best_remains_messages"))}</td>'
            f'<td class="mono">{escape_html(window.get("cannot_continue_messages"))}</td>'
            f"<td>{render_status(window.get('outcome'))}</td>"
            "</tr>"
        )
    window_table = (
        '<details class="summary-block"><summary>Per-stop continuation windows '
        f'({len(window_rows)})</summary><div><div class="table-scroll"><table class="hook-table"><thead><tr>'
        "<th>Candidate</th><th>Loop agent</th><th>Stop #</th><th>Decision</th><th>Started</th>"
        "<th>Context</th><th>Evidence</th><th>Edit-capable</th><th>Verifier</th>"
        "<th>Revision changes</th><th>Improvements</th><th>Assistant</th><th>Empty</th>"
        "<th>Best-remains</th><th>Cannot-continue</th><th>Outcome</th>"
        f"</tr></thead><tbody>{''.join(window_rows)}</tbody></table></div></div></details>"
        if window_rows
        else '<p class="footnote">No per-invocation SubagentStop window was captured.</p>'
    )
    return (
        summary_table
        + window_table
        + '<p class="footnote">Final role is a report-time incumbent/follower label, not a causal claim. '
        "A productive block requires a blocked SubagentStop followed before the next stop by a verifier "
        "on a different immutable Git revision. Context and global-evidence counts come from content-free "
        "native Codex activity events. Tail signals are deterministic heuristics over the period after the "
        "last verifier; unavailable evidence remains Not observed.</p>"
    )


def _render_candidate_loop_statistics(
    activity: dict[str, Any],
    stop_hook_statistics: dict[str, Any],
) -> str:
    hook_decisions = stop_hook_statistics.get("by_decision") or {}
    hook_by_event = stop_hook_statistics.get("by_event") or {}
    stop_blocks = (hook_by_event.get("Stop") or {}).get("decisions") or {}
    subagent_blocks = (hook_by_event.get("SubagentStop") or {}).get("decisions") or {}
    requested = {
        "candidate_submissions": activity.get("candidates_submitted", 0),
        "completed_with_result": activity.get("candidates_completed_with_result", 0),
        "rejected_results": activity.get("results_rejected", 0),
        "agent_resumes": activity.get("agent_resumes", 0),
        "stop_hook_continue_triggers": hook_decisions.get("block", 0),
    }
    result_details = {
        "durable_results": activity.get("results_total", 0),
        "kept_results": activity.get("results_kept", 0),
        "rejected_results": activity.get("results_rejected", 0),
        "unsettled_results": activity.get("results_unsettled", 0),
    }
    continuation_details = {
        "same_session_resumes": activity.get("same_session_resumes", 0),
        "redispatch_resumes": activity.get("redispatch_resumes", 0),
        "top_level_stop_triggers": stop_blocks.get("block", 0),
        "subagent_stop_triggers": subagent_blocks.get("block", 0),
    }
    return (
        '<div class="stats-grid">'
        '<div class="stats-table"><h3>Requested Candidate Metrics</h3>'
        f"{_stat_rows(requested)}</div>"
        '<div class="stats-table"><h3>Result Settlement</h3>'
        f"{_stat_rows(result_details)}</div>"
        '<div class="stats-table"><h3>Continuation Sources</h3>'
        f"{_stat_rows(continuation_details)}</div>"
        "</div>"
    )


def _render_artifact_lifecycle(task: dict[str, Any]) -> str:
    resources = task.get("artifact_availability") or []
    if not resources:
        return ""
    rows = []
    for resource in resources:
        artifact_ref = resource.get("artifact_ref") or {}
        availability = str(resource.get("artifact_availability") or "unknown")
        rows.append(
            "<tr>"
            f'<td class="mono">{escape_html(artifact_ref.get("id"))}</td>'
            f"<td>{render_status(availability)}</td>"
            f'<td class="mono">{escape_html(artifact_ref.get("provider"))}</td>'
            "</tr>"
        )
    content = (
        "<p>Artifact identity and saved Evidence/Annotation results remain durable. "
        "Removed snapshot contents cannot be reopened as an Artifact View.</p>"
        '<div class="table-scroll"><table><thead><tr>'
        "<th>Artifact</th><th>Availability</th><th>Provider</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )
    return (
        '<section class="subsection"><h3>Provider Artifact Lifecycle</h3>'
        f"{content}</section>"
    )


def _render_task(
    task: dict[str, Any],
    index: int,
    *,
    trajectory_payload: dict[str, Any] | None = None,
) -> str:
    run = task.get("run") or {}
    frozen = task.get("frozen_spec") or {}
    stats = task.get("statistics") or {}
    scores = stats.get("scores") or {}
    timing = stats.get("timing") or {}
    score_target = scores.get("target")
    score_target_text = (
        "Not configured" if score_target is None else numeric_value(score_target, digits=4)
    )
    run_id = str(task.get("run_id") or f"task-{index}")
    selected = task.get("is_report_run")
    trajectory = (
        _render_search_trajectory(trajectory_payload)
        if trajectory_payload is not None
        else ""
    )
    return (
        f'<article class="panel task-panel" data-run-id="{escape(run_id, quote=True)}" '
        f'id="task-{escape(run_id, quote=True)}" {"" if selected else "hidden"}>'
        '<header class="task-head">'
        '<div class="task-title-line">'
        f'<h3>Search Task {index:02d}: <span class="mono">{escape(run_id)}</span></h3>'
        f"{render_status(run.get('state') or task.get('state'))}"
        "</div>"
        f'<p class="task-objective">{escape_html(frozen.get("objective"))}</p>'
        "</header>"
        '<div class="task-metrics">'
        f'<div class="task-metric"><span class="kpi-label">Goal revision</span><strong>{escape_html(task.get("goal_revision"))}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">Strategy</span><strong>{escape_html((task.get("strategy") or {}).get("name"))}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">Orchestration</span><strong>{escape_html((task.get("strategy") or {}).get("orchestration_mode"))}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">Baseline</span><strong class="mono">{escape_html(numeric_value(scores.get("baseline")))}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">Score target</span><strong class="mono">{escape(score_target_text)}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">Best / selected</span><strong class="mono">{escape_html(numeric_value(scores.get("best")))} / {escape_html(numeric_value(scores.get("selected")))}</strong></div>'
        f'<div class="task-metric"><span class="kpi-label">First improvement</span><strong class="mono">{escape_html(format_duration(timing.get("time_to_first_improvement_seconds")))}</strong></div>'
        "</div>"
        '<section class="subsection">'
        f"{trajectory}"
        f"{_render_timeline(task.get('timeline') or {}, title='Search Execution Timeline', include_score_chart=not bool(trajectory))}"
        '<p class="footnote">This axis is scoped to this Search run. Worker bars show actual host execution, not the configured maximum or an aspirational exploration window.</p>'
        "</section>"
        '<section class="subsection"><h3>Bootstrap Verification</h3>'
        f"{_render_bootstrap_verification(task)}</section>"
        '<section class="subsection"><h3>Shared Evidence View</h3>'
        f"{_render_shared_evidence_view(task)}</section>"
        '<section class="subsection"><h3>Candidate Evidence</h3>'
        f"{_render_candidates(task)}</section>"
        f"{_render_artifact_lifecycle(task)}"
        '<section class="subsection"><h3>Worker Sessions</h3>'
        f"{_render_sessions(task)}</section>"
        '<section class="subsection"><h3>Complete Statistical View</h3>'
        f"{_render_statistics(task)}"
        '<details class="summary-block"><summary>Raw normalized Search statistics</summary>'
        f"<pre>{escape(json.dumps(stats, indent=2, ensure_ascii=False, sort_keys=True))}</pre></details>"
        "</section></article>"
    )


def _render_bootstrap_verification(task: dict[str, Any]) -> str:
    frozen = task.get("frozen_spec") or {}
    run = task.get("run") or {}
    verifications = [
        frozen.get("origin_verification"),
        run.get("workspace_bootstrap_verification"),
    ]
    rows = []
    for verification in verifications:
        if not isinstance(verification, dict):
            continue
        artifact = verification.get("source_artifact_ref") or {}
        results = verification.get("verifier_results") or []
        verifier_summary = ", ".join(
            f"{result.get('name')}={'pass' if result.get('passed') else 'fail'}"
            for result in results
            if isinstance(result, dict)
        )
        rows.append(
            "<tr>"
            f"<td><code>{escape_html(verification.get('phase'))}</code></td>"
            f"<td><code>{escape_html(verification.get('workspace_provider'))}</code></td>"
            f"<td><code>{escape_html(verification.get('candidate_id'))}</code></td>"
            f"<td>{render_status('passed' if verification.get('passed') else 'failed')}</td>"
            f'<td class="mono">{escape_html(numeric_value(verification.get("aggregate_score"), digits=4))}</td>'
            f"<td><code>{escape_html(artifact.get('id'))}</code></td>"
            f"<td>{escape_html(verifier_summary)}</td>"
            "</tr>"
        )
    if not rows:
        return (
            "<p>No origin or workspace-relocation bootstrap verification was "
            "recorded for this run.</p>"
        )
    return (
        "<p>These control-plane gates are separate from candidate iterations "
        "and ranking.</p>"
        '<div class="table-scroll"><table><thead><tr>'
        "<th>Phase</th><th>Backend</th><th>Candidate</th><th>Status</th>"
        "<th>Score</th><th>Source artifact</th><th>Verifiers</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def _render_agent_transcript_section(agent_transcript: Any) -> str:
    """Optional closing link to a local debug transcript export; empty when absent."""
    if not isinstance(agent_transcript, dict):
        return ""
    href = agent_transcript.get("href")
    if not isinstance(href, str) or not href:
        return ""
    return f"""    <section id="agent-transcript" class="report-section">
      <h2>Agent Behavior Transcript</h2>
      <p><a class="mono" href="{escape(href)}">{escape(href)}</a></p>
      <p class="footnote">Local debug export of the full main/subagent session histories, produced by scripts/export_agent_transcript.py. It may contain prompts and tool payloads, lives outside this report's normalized evidence, and only exists while kept under host-logs.</p>
    </section>
"""


def render_html_report(
    data: dict[str, Any], *, plotly_javascript: str | None = None
) -> str:
    snapshot = data.get("snapshot") or {}
    goal = snapshot.get("goal_plus") or {}
    aggregate = snapshot.get("search_task_aggregate") or {}
    aggregate_stats = aggregate.get("statistics") or {}
    candidate_activity = aggregate_stats.get("activity") or {}
    aggregate_usage = aggregate_stats.get("usage") or {}
    total_statistics = snapshot.get("statistics") or {}
    total_usage = total_statistics.get("total_usage") or {}
    orchestrator = total_statistics.get("orchestrator") or {}
    orchestrator_usage = orchestrator.get("usage") or {}
    annotator_usage = total_statistics.get("annotator_usage") or {}
    tasks = data.get("search_tasks") or []
    report_run_id = str(data.get("report_run_id") or "unknown")
    selected_task = next(
        (task for task in tasks if task.get("is_report_run")),
        tasks[-1] if tasks else {},
    )
    selected_frozen = selected_task.get("frozen_spec") or {}
    selected_stats = selected_task.get("statistics") or {}
    selected_scores = selected_stats.get("scores") or {}
    unavailable = total_statistics.get("unavailable_metrics") or []
    missing = (selected_stats.get("data_quality") or {}).get("missing") or []
    warnings = snapshot.get("warnings") or []
    stop_hook_statistics = data.get("stop_hook_statistics") or {}
    loop_agent_statistics = data.get("loop_agent_statistics") or {}
    goal_id = data.get("goal_plus_id")
    title_id = str(goal_id or report_run_id)
    state = goal.get("status") or (selected_task.get("run") or {}).get("state")
    search_count = aggregate.get("search_tasks_total", len(tasks))
    run_state = (selected_task.get("run") or {}).get("state") or selected_task.get(
        "state"
    )
    score_target = selected_scores.get("target")
    score_gain = selected_scores.get("selected_improvement_from_baseline")
    score_baseline = finite_float(selected_scores.get("baseline"))
    score_detail = (
        "No baseline / gain No baseline"
        if score_baseline is None
        else f"baseline {numeric_value(score_baseline, digits=4)} / "
        f"gain {_metric_readout('score_gain', score_gain)}"
    )
    completion_detail = f"run {str(run_state or 'unknown').lower()}"
    selected_run = selected_task.get("run") or {}
    search_result = selected_run.get("search_result") or "unknown"
    publication_outcome = selected_run.get("publication_outcome") or {}
    publication_state = publication_outcome.get("state") or "not_applicable"
    publication_result = (
        "pending_integration"
        if publication_state == "awaiting_main_integration"
        else publication_state
    )
    if score_target is None:
        completion_detail += " / no score threshold"
        completion_semantics = (
            "No score threshold was configured for this Search task. Complete means the Goal Plus "
            "workflow finished and the selected candidate survived the required verification and promotion; "
            "it does not claim that an unspecified threshold was reached."
        )
        score_target_text = "Not configured"
    else:
        target_reached = selected_scores.get("target_reached")
        target_result = (
            "reached"
            if target_reached is True
            else "not reached"
            if target_reached is False
            else "not evaluated"
        )
        completion_detail += f" / target {target_result}"
        completion_semantics = (
            f"A score threshold of {numeric_value(score_target, digits=4)} was configured and was "
            f"{target_result} by the selected result."
        )
        score_target_text = numeric_value(score_target, digits=4)

    usage_complete = bool(total_usage.get("complete"))
    kpis = "".join(
        [
            _metric_card(
                "Goal status",
                format_text(state).title(),
                completion_detail,
                status_class(state),
            ),
            _metric_card(
                "Search result",
                format_text(search_result).replace("_", " ").title(),
                "candidate verification",
                "success" if search_result == "passed" else "",
            ),
            _metric_card(
                "Publication result",
                format_text(publication_result).replace("_", " ").title(),
                "workspace integration",
                "success" if publication_result == "applied" else "",
            ),
            _metric_card("Search tasks", numeric_value(search_count), "GP-level tasks"),
            _metric_card(
                "Selected score",
                numeric_value(selected_scores.get("selected"), digits=4),
                score_detail,
                "success",
            ),
            _metric_card(
                "Candidates",
                numeric_value(aggregate.get("candidates_total")),
                f"{numeric_value(aggregate.get('candidates_evaluated'))} evaluated",
            ),
            _metric_card(
                "Worker sessions",
                numeric_value(aggregate.get("worker_sessions_total")),
                f"{numeric_value((aggregate_stats.get('workers') or {}).get('timed_out'))} timed out",
            ),
            _metric_card(
                "Verifier runs",
                numeric_value(aggregate.get("verifier_runs_total")),
                f"{numeric_value((aggregate_stats.get('verifiers') or {}).get('parent_process_runs'))} parent-owned",
            ),
            _metric_card(
                "Total processed tokens"
                if usage_complete
                else "Known processed tokens",
                numeric_value(total_usage.get("processed_tokens")),
                "workers + orchestrator + annotator",
            ),
            _metric_card(
                "Estimated total cost" if usage_complete else "Known estimated cost",
                format_cost(total_usage.get("cost_usd")),
                "workers + orchestrator + annotator",
            ),
        ]
    )

    trajectory_payloads = {
        str(task.get("run_id")): payload
        for task in tasks
        if (payload := _search_trajectory_payload(task)) is not None
    }
    plotly_script = f"<script>{plotly_javascript}</script>" if plotly_javascript else ""

    task_tabs = "".join(
        f'<button class="task-tab" type="button" data-task-target="{escape(str(task.get("run_id")), quote=True)}" '
        f'aria-selected="{"true" if task.get("is_report_run") else "false"}">'
        f'<span>Task {index:02d}</span><span class="mono">r{escape_html(task.get("goal_revision"))}</span>'
        f"{render_status((task.get('run') or {}).get('state'))}</button>"
        for index, task in enumerate(tasks, start=1)
    )
    task_panels = "".join(
        _render_task(
            task,
            index,
            trajectory_payload=(
                trajectory_payloads.get(str(task.get("run_id")))
                if plotly_javascript
                else None
            ),
        )
        for index, task in enumerate(tasks, start=1)
    )
    warning_items = (
        "".join(
            f'<li><span class="mono">{escape_html(item.get("kind") if isinstance(item, dict) else "warning")}</span>: '
            f"{escape_html(json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else item)}</li>"
            for item in warnings
        )
        or "<li>No monitor warnings.</li>"
    )
    metric_availability = _render_metric_availability(unavailable + missing)
    agent_transcript_section = _render_agent_transcript_section(data.get("agent_transcript"))

    worker_sources = int(aggregate_usage.get("sources_total") or 0)
    worker_coverage = (aggregate_usage.get("coverage") or {}).get(
        "processed_tokens"
    ) or 0
    coverage_percent = (
        min(100.0, worker_coverage / worker_sources * 100) if worker_sources else 0.0
    )
    raw_payload = {
        "schema_version": data.get("schema_version"),
        "generated_at": data.get("generated_at"),
        "snapshot": snapshot,
        "search_tasks": tasks,
        "timeline": data.get("timeline"),
        "stop_hook_statistics": stop_hook_statistics,
        "loop_agent_statistics": loop_agent_statistics,
    }

    print_icon = (
        '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>'
        '<rect width="12" height="8" x="6" y="14"/></svg>'
    )

    return f"""<!doctype html>
<html lang="en" data-report-schema="goal-plus-report/v{REPORT_SCHEMA_VERSION}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>Goal Plus Execution Report: {escape(title_id)}</title>
  <script>document.documentElement.classList.add('js');</script>
  <style>{REPORT_CSS}</style>
</head>
<body data-goal-plus-id="{escape(str(goal_id or ""), quote=True)}" data-run-id="{escape(report_run_id, quote=True)}">
  <header class="masthead">
    <div class="wrap masthead-inner">
      <div class="identity">
        <div class="eyebrow">Goal Plus Execution Report</div>
        <div class="identity-line"><h1>{escape(title_id)}</h1>{render_status(state)}</div>
        <div class="id-line mono">Report run: {escape(report_run_id)}</div>
      </div>
      <div class="masthead-actions">
        <div class="generated">Generated<strong class="mono">{escape_html(data.get("generated_at"))}</strong></div>
        <button class="button no-print" type="button" onclick="window.print()" title="Print report">{print_icon}<span>Print</span></button>
      </div>
    </div>
  </header>
  <nav class="section-nav no-print" aria-label="Report sections">
    <div class="wrap">
      <a href="#aggregate">Summary</a><a href="#goal">Goal</a>
      <a href="#hooks">Candidate / hooks</a>
      <a href="#tasks">Search tasks ({escape(numeric_value(search_count))})</a><a href="#audit">Audit</a>
    </div>
  </nav>
  <main class="wrap">
    <section id="aggregate" class="report-section">
      <div class="section-kicker">Goal Plus Summary</div>
      <div class="kpi-grid">{kpis}</div>
    </section>
    <section id="goal" class="report-section">
      <h2>Goal And Completion</h2>
      <div class="panel panel-body">
        <h3>Selected Search Objective</h3>
        <p class="objective">{escape_html(selected_frozen.get("objective"))}</p>
      </div>
      <div class="panel panel-body completion-note">
        <h3>Completion semantics</h3>
        <p>{escape(completion_semantics)}</p>
      </div>
      <div class="panel panel-body">
        <h3>Goal Record</h3>
        <dl class="fact-grid">
          <div class="fact"><dt>Goal revision</dt><dd>{escape_html(goal.get("goal_revision"))}</dd></div>
          <div class="fact"><dt>Phase</dt><dd>{escape_html(goal.get("phase"))}</dd></div>
          <div class="fact"><dt>Selection survival</dt><dd>{escape_html(format_percent(aggregate_stats.get("selection_survival_rate")))}</dd></div>
          <div class="fact"><dt>Score target</dt><dd>{escape(score_target_text)}</dd></div>
        </dl>
        <details class="summary-block"><summary>Original raw goal</summary><div class="raw-goal">{escape_html(goal.get("raw_goal"))}</div></details>
      </div>
      <div class="panel panel-body">
        <h3>Usage Coverage</h3>
        <div class="coverage-row"><span>Worker processed-token coverage</span><strong>{worker_coverage}/{worker_sources} sessions</strong></div>
        <div class="coverage-bar" aria-label="Worker usage coverage"><span style="width:{coverage_percent:.2f}%"></span></div>
        <dl class="fact-grid coverage">
          <div class="fact"><dt>Worker processed tokens</dt><dd class="mono">{escape_html(numeric_value(aggregate_usage.get("processed_tokens")))}</dd></div>
          <div class="fact"><dt>Orchestrator processed tokens</dt><dd class="mono">{escape_html(numeric_value(orchestrator_usage.get("processed_tokens")))}</dd></div>
          <div class="fact"><dt>Annotator processed tokens</dt><dd class="mono">{escape_html(numeric_value(annotator_usage.get("processed_tokens")))}</dd></div>
          <div class="fact"><dt>Combined known tokens</dt><dd class="mono">{escape_html(numeric_value(total_usage.get("processed_tokens")))}</dd></div>
          <div class="fact"><dt>Combined estimated cost</dt><dd class="mono">{escape_html(format_cost(total_usage.get("cost_usd")))}</dd></div>
        </dl>
      </div>
    </section>
    <section id="hooks" class="report-section">
      <div class="section-kicker">Candidate Loop And Host Hook Evidence</div>
      <div class="activity-summary">
      <h2>Candidate Loop Activity</h2>
      {_render_candidate_loop_statistics(candidate_activity, stop_hook_statistics)}
      <p class="footnote">Candidate submissions count durable candidate records. Completed with Result counts unique candidates with at least one verifier-settled iteration. Rejected Results are discard/failure dispositions. Agent resumes combine accepted same-session continuations and state redispatches. Stop-hook continue triggers count automatic Stop/SubagentStop block decisions.</p>
      </div>
      <div class="activity-summary">
      <h2>Loop Agent Stop Outcomes</h2>
      {_render_loop_agent_statistics(loop_agent_statistics)}
      </div>
      <h2>Stop Hook Activity</h2>
      {_render_stop_hook_statistics(stop_hook_statistics)}
      <p class="footnote">This is a static snapshot of durable automatic Stop and SubagentStop invocations available when the report was generated. Direct goal_plus_gate calls are excluded.</p>
    </section>
    <section id="tasks" class="report-section">
      <div class="section-kicker">Per-Task Evidence</div>
      <h2>Search Tasks</h2>
      <div class="task-tabs no-print" role="tablist" aria-label="Search tasks">{task_tabs}</div>
      {task_panels or '<div class="panel panel-body">No linked Search tasks were found.</div>'}
    </section>
    <section id="audit" class="report-section">
      <h2>Report Audit</h2>
      <div class="two-column">
        <div class="panel panel-body"><h3>Monitor Warnings</h3><ul class="warning-list">{warning_items}</ul></div>
        <div class="panel panel-body"><h3>Timeline Gate Summary</h3>{_stat_rows((data.get("timeline") or {}).get("gate_events") or {}) or "<p>No gate events observed.</p>"}</div>
      </div>
      {metric_availability}
      <details class="summary-block"><summary>Complete normalized report data</summary><pre>{escape(json.dumps(raw_payload, indent=2, ensure_ascii=False, sort_keys=True))}</pre></details>
      <p class="footnote">Goal Plus {escape(__version__)} · Schema goal-plus-report/v{REPORT_SCHEMA_VERSION}. This file is self-contained and generated from durable Goal Plus/Search state. Host-native transcripts remain external evidence and are summarized only through normalized observability.</p>
    </section>
{agent_transcript_section}
  </main>
  {plotly_script}
  <script>{REPORT_SCRIPT}</script>
</body>
</html>
"""


def _codex_report_hook_statistics(observation: dict[str, Any]) -> dict[str, Any]:
    existing = observation.get("hook_statistics")
    if isinstance(existing, dict):
        return existing

    hooks = observation.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    by_decision = stop_hook_decision_counts()
    by_event: dict[str, dict[str, Any]] = {}
    events_total = 0
    for event_name, source_name in (
        ("Stop", "stop"),
        ("SubagentStop", "subagent_stop"),
    ):
        source = hooks.get(source_name)
        source = source if isinstance(source, dict) else {}
        started = int(source.get("started") or 0)
        block = int(source.get("blocked") or 0)
        allow = int(source.get("completed_or_allowed") or 0)
        error = int(source.get("other_terminal_statuses") or 0)
        unknown = max(0, started - block - allow - error)
        decisions = {
            "block": block,
            "allow": allow,
            "skipped": 0,
            "error": error,
            "unknown": unknown,
        }
        by_event[event_name] = {
            "events_total": started,
            "duration_ms_total": None,
            "decisions": decisions,
        }
        events_total += started
        for decision, count in decisions.items():
            by_decision[decision] += count
    return {
        "schema_version": 1,
        "events_total": events_total,
        "duration_ms_total": None,
        "captured_through": None,
        "by_event": by_event,
        "by_decision": by_decision,
        "subagents": [],
        "events": [],
    }


def _codex_trajectory_payload(observation: dict[str, Any]) -> dict[str, Any] | None:
    evaluations = observation.get("evaluations")
    evaluations = evaluations if isinstance(evaluations, dict) else {}
    entries = evaluations.get("entries")
    entries = entries if isinstance(entries, list) else []
    if not entries:
        return None

    metric_name = str(evaluations.get("metric_name") or "score")
    direction = str(evaluations.get("metric_direction") or "maximize")
    if direction not in {"minimize", "maximize"}:
        direction = "maximize"

    normalized: list[dict[str, Any]] = []
    for call, raw_entry in enumerate(entries, start=1):
        if not isinstance(raw_entry, dict):
            continue
        score = finite_float(raw_entry.get("score"))
        round_name = str(raw_entry.get("round") or f"submission-{call}")
        kind = str(raw_entry.get("kind") or "submission")
        normalized.append(
            {
                "call": call,
                "round": round_name,
                "kind": kind,
                "score": score,
                "valid": raw_entry.get("valid") is not False and score is not None,
                "at": raw_entry.get("at"),
            }
        )
    scored = [entry for entry in normalized if entry["score"] is not None]
    if not scored:
        return None

    trajectories: list[dict[str, Any]] = []
    kinds = list(dict.fromkeys(str(entry["kind"]) for entry in scored))
    for kind in kinds:
        points = [entry for entry in scored if entry["kind"] == kind]
        passing = [entry for entry in points if entry["valid"]]
        failed = [entry for entry in points if not entry["valid"]]
        trajectories.append(
            {
                "candidate_id": f"{kind} submissions",
                "selected": any(
                    entry["round"] == evaluations.get("best_round") for entry in points
                ),
                "calls": [entry["call"] for entry in passing],
                "scores": [entry["score"] for entry in passing],
                "details": [
                    [
                        entry["round"],
                        f"{kind} submission",
                        str(entry.get("at") or "timestamp unavailable"),
                    ]
                    for entry in passing
                ],
                "failed_calls": [entry["call"] for entry in failed],
                "failed_scores": [entry["score"] for entry in failed],
                "failed_details": [
                    [
                        entry["round"],
                        f"{kind} submission",
                        str(entry.get("at") or "timestamp unavailable"),
                    ]
                    for entry in failed
                ],
            }
        )

    global_calls: list[int] = []
    global_scores: list[float] = []
    current: float | None = None
    for entry in normalized:
        if not entry["valid"]:
            continue
        score = float(entry["score"])
        if current is None or is_better_score(score, current, direction):
            current = score
        global_calls.append(int(entry["call"]))
        global_scores.append(float(current))

    best_round = evaluations.get("best_round")
    selected_entry = next(
        (
            entry
            for entry in normalized
            if entry["valid"] and entry["round"] == best_round
        ),
        None,
    )
    selected_point = (
        {
            "candidate_id": f"{selected_entry['kind']} submissions",
            "call": selected_entry["call"],
            "score": selected_entry["score"],
        }
        if selected_entry is not None
        else None
    )
    passing_scores = [float(entry["score"]) for entry in normalized if entry["valid"]]
    passing_count = sum(entry["valid"] for entry in normalized)
    return {
        "title": "Submission Score Trajectory",
        "aria_subject": "Codex submission score trajectory",
        "unit_label": "submissions",
        "group_label": "series",
        "call_label": "Submission",
        "point_label": "Round",
        "best_label": "Best-so-far",
        "failure_label": "invalid submission",
        "failure_legend": "Invalid submission · not ranked",
        "export_name": "codex-run-submission-trajectory",
        "metric_name": metric_name,
        "metric_direction": direction,
        "baseline": None,
        "selected": finite_float(evaluations.get("best_score")),
        "evaluations": len(entries),
        "passing_evaluations": passing_count,
        "failed_evaluations": len(entries) - passing_count,
        "call_window": _trajectory_call_window(len(entries)),
        "score_axis": _trajectory_score_axis(passing_scores),
        "trajectories": trajectories,
        "global_best": {"calls": global_calls, "scores": global_scores},
        "selected_point": selected_point,
    }


def _render_stop_progress_chart(observation: dict[str, Any]) -> str:
    hooks = observation.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    stop = hooks.get("stop")
    stop = stop if isinstance(stop, dict) else {}
    raw_bins = stop.get("blocked_output_progress_bins")
    bins = [int(value or 0) for value in raw_bins] if isinstance(raw_bins, list) else []
    if not bins:
        return '<p class="footnote">No output-progress distribution was persisted.</p>'

    width = 1000.0
    height = 210.0
    chart_top = 18.0
    chart_bottom = 178.0
    chart_height = chart_bottom - chart_top
    maximum = max(bins) or 1
    step = width / len(bins)
    gap = min(2.0, step * 0.18)
    bars = []
    for index, count in enumerate(bins):
        bar_height = chart_height * count / maximum
        x = index * step + gap / 2
        y = chart_bottom - bar_height
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(0.5, step - gap):.2f}" '
            f'height="{bar_height:.2f}" fill="var(--accent)">'
            f"<title>Output progress bin {index + 1}: {count} blocked Stop attempts</title>"
            "</rect>"
        )
    return (
        '<div class="stop-progress-chart">'
        f'<svg viewBox="0 0 1000 {height:.0f}" preserveAspectRatio="none" '
        'role="img" aria-label="Blocked Stop attempts by execution output progress">'
        f'<line x1="0" y1="{chart_bottom:.2f}" x2="{width:.2f}" y2="{chart_bottom:.2f}" '
        'stroke="var(--border-strong)" stroke-width="1"/>'
        + "".join(bars)
        + f'<text x="4" y="12" fill="var(--muted)" font-size="10">max bin {maximum}</text>'
        "</svg>"
        '<div class="stop-progress-axis"><span>0% output</span>'
        "<span>Execution-output progress, not wall-clock time</span>"
        "<span>100% output</span></div></div>"
    )


def _render_codex_execution_timeline(observation: dict[str, Any]) -> str:
    run = observation.get("run")
    run = run if isinstance(run, dict) else {}
    run_log = observation.get("run_log")
    run_log = run_log if isinstance(run_log, dict) else {}
    evaluations = observation.get("evaluations")
    evaluations = evaluations if isinstance(evaluations, dict) else {}
    entries = evaluations.get("entries")
    entries = entries if isinstance(entries, list) else []
    started_at = run_log.get("first_timestamp") or run.get("started_at")
    ended_at = run_log.get("last_timestamp") or run.get("ended_at")
    start_epoch = parse_epoch(started_at)
    end_epoch = parse_epoch(ended_at)
    if start_epoch is None or end_epoch is None or end_epoch <= start_epoch:
        return (
            '<div class="panel panel-body"><p>Execution start/end timestamps were not '
            "available for a wall-clock timeline.</p></div>"
        )

    span = end_epoch - start_epoch
    auto_points = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "auto":
            continue
        timestamp = entry.get("at")
        event_epoch = parse_epoch(timestamp)
        if event_epoch is None:
            continue
        left = max(0.0, min(99.0, (event_epoch - start_epoch) / span * 100.0))
        auto_points.append(
            f'<span class="timeline-event parent point" style="left:{left:.3f}%;width:0.8%;" '
            f'title="{escape(str(entry.get("round") or "auto evaluation"), quote=True)} · '
            f'{escape(str(timestamp), quote=True)}"></span>'
        )
    terminal_state = str(
        run.get("terminal_state")
        or ("timed_out" if run.get("timed_out") else "completed")
    )
    terminal_label = terminal_state.replace("_", " ")
    return (
        '<div class="panel timeline-shell">'
        '<div class="timeline-head"><h2>Codex Execution Timeline</h2>'
        f'<span class="mono">Observed span: {escape(format_duration(span))}</span></div>'
        '<div class="timeline-scroll" tabindex="0" aria-label="Codex execution timeline">'
        '<div class="timeline" style="--timeline-width:980px">'
        '<div class="timeline-rows" data-track-count="2">'
        '<div class="timeline-row"><div class="timeline-label">Codex process</div>'
        '<div class="timeline-track"><span class="timeline-event main" '
        f'style="left:0%;width:100%;" title="Codex execution · {escape(terminal_label, quote=True)}">'
        f"Codex execution · {escape(terminal_label)}</span></div></div>"
        '<div class="timeline-row"><div class="timeline-label">Auto evaluations</div>'
        f'<div class="timeline-track">{"".join(auto_points)}</div></div>'
        "</div>"
        '<div class="timeline-axis">'
        f"<span>{escape(str(started_at))}</span><span>+{escape(format_duration(span / 2))}</span>"
        f"<span>{escape(str(ended_at))}</span></div></div></div>"
        '<div class="timeline-key"><span><i class="key-dot"></i>Codex process</span>'
        '<span><i class="key-dot parent"></i>Timestamped auto evaluation</span>'
        "<span>Agent submissions without persisted timestamps remain in submission order only.</span>"
        "</div></div>"
    )


def _render_codex_submissions(observation: dict[str, Any]) -> str:
    evaluations = observation.get("evaluations")
    evaluations = evaluations if isinstance(evaluations, dict) else {}
    entries = evaluations.get("entries")
    entries = entries if isinstance(entries, list) else []
    rows = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        passed = entry.get("passed")
        total_tests = entry.get("total_tests")
        test_result = (
            f"{passed}/{total_tests}"
            if isinstance(passed, int) and isinstance(total_tests, int)
            else "Not observed"
        )
        rows.append(
            "<tr>"
            f'<td class="mono">{index}</td>'
            f'<td class="mono"><strong>{escape_html(entry.get("round"))}</strong></td>'
            f"<td>{escape_html(entry.get('kind'))}</td>"
            f'<td class="mono">{escape_html(numeric_value(entry.get("score"), digits=4))}</td>'
            f'<td class="mono">{escape_html(format_percent(entry.get("pass_rate")))}</td>'
            f"<td>{escape_html(test_result)}</td>"
            f"<td>{escape_html('Yes' if entry.get('valid') is True else 'No')}</td>"
            f'<td class="mono">{escape_html(entry.get("at"))}</td>'
            f"<td>{escape_html(entry.get('summary'))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p>No persisted submission history was found.</p>"
    return (
        '<div class="table-scroll"><table><thead><tr>'
        "<th>#</th><th>Round</th><th>Kind</th><th>Score</th><th>Pass rate</th>"
        "<th>Tests</th><th>Valid</th><th>Timestamp</th><th>Summary</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _artifact_link(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "Not observed"
    path = Path(value)
    try:
        uri = path.as_uri()
    except ValueError:
        return escape_html(value)
    return f'<a href="{escape(uri, quote=True)}">{escape_html(path.name)}</a>'


def _render_codex_goal_plus_finalization(observation: dict[str, Any]) -> str:
    goal_plus = observation.get("goal_plus")
    if not isinstance(goal_plus, dict) or not goal_plus.get("source_available"):
        return ""
    records = goal_plus.get("records")
    records = records if isinstance(records, list) else []
    rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        search_tasks = record.get("search_tasks")
        search_tasks = search_tasks if isinstance(search_tasks, list) else []
        search_state = (
            ", ".join(
                f"{task.get('run_id')}={task.get('state') or 'unknown'}"
                for task in search_tasks
                if isinstance(task, dict)
            )
            or "None"
        )
        rows.append(
            "<tr>"
            f'<td class="mono"><strong>{escape_html(record.get("goal_plus_id"))}</strong></td>'
            f"<td>{render_status(record.get('status'))}</td>"
            f"<td>{escape_html(record.get('phase'))}</td>"
            f"<td>{escape_html(record.get('next_action'))}</td>"
            f"<td>{escape_html(record.get('result_recorded_at'))}</td>"
            f'<td class="mono">{escape_html(record.get("selected_candidate_id"))}</td>'
            f'<td class="mono">{escape_html(search_state)}</td>'
            "</tr>"
        )
    overall = str(goal_plus.get("overall_status") or "unknown")
    overall_label = {
        "complete": "Complete",
        "incomplete": "Incomplete",
        "unavailable": "Not observed",
    }.get(overall, overall.replace("_", " ").title())
    tone = (
        "success"
        if overall == "complete"
        else "failure"
        if overall == "incomplete"
        else "warning"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr>'
        "<th>Goal</th><th>Status</th><th>Phase</th><th>Next action</th>"
        "<th>Result recorded</th><th>Selected candidate</th><th>Search runs</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        if rows
        else "<p>No Goal Plus records were found in the persisted state archive.</p>"
    )
    return (
        '<section id="goal-plus-finalization" class="report-section">'
        '<div class="section-kicker">Goal Plus Durable State</div>'
        '<div class="kpi-grid">'
        f"{_metric_card('Goal Plus finalization', overall_label, f'{len(records)} persisted record(s)', tone)}"
        f"{_metric_card('Active records', numeric_value(goal_plus.get('active_records')), 'must be zero for a final Goal Plus report', 'failure' if goal_plus.get('active_records') else 'success')}"
        f"{_metric_card('Terminal records', numeric_value(goal_plus.get('terminal_records')), 'complete / abandoned / closed legacy records')}"
        f"{_metric_card('Persisted reports', numeric_value(goal_plus.get('reports_generated')), 'terminal Goal Plus report paths')}"
        "</div>"
        '<div class="panel panel-body">'
        "<h3>Goal Plus Finalization Evidence</h3>"
        "<p>Benchmark evaluator outcome and Goal Plus lifecycle completion are "
        "independent facts. A valid promoted result does not make an active Goal "
        "record terminal.</p>"
        f"{table}</div></section>"
    )


def render_codex_observability_report(
    observation: dict[str, Any], *, plotly_javascript: str | None = None
) -> str:
    run = observation.get("run")
    run = run if isinstance(run, dict) else {}
    result = observation.get("result")
    result = result if isinstance(result, dict) else {}
    usage = observation.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    evaluations = observation.get("evaluations")
    evaluations = evaluations if isinstance(evaluations, dict) else {}
    availability = observation.get("availability")
    availability = availability if isinstance(availability, dict) else {}
    evidence = observation.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    warnings = observation.get("warnings")
    warnings = warnings if isinstance(warnings, list) else []
    hook_statistics = _codex_report_hook_statistics(observation)
    stop = (observation.get("hooks") or {}).get("stop") or {}
    stop_observed = availability.get("stop_hook_events")
    if not isinstance(stop_observed, bool):
        stop_observed = bool(hook_statistics.get("events_total"))
    hook_activity_html = (
        _render_stop_hook_statistics(hook_statistics)
        if stop_observed
        else (
            '<div class="panel panel-body"><p>Stop/SubagentStop activity was not '
            "observed because no structured hook evidence or hook lifecycle output "
            "was persisted for this run.</p></div>"
        )
    )
    stop_progress_html = (
        _render_stop_progress_chart(observation)
        if stop_observed
        else '<p class="footnote">No persisted hook evidence was available.</p>'
    )
    trajectory_payload = _codex_trajectory_payload(observation)
    trajectory_html = (
        _render_search_trajectory(trajectory_payload)
        if trajectory_payload is not None and plotly_javascript
        else (
            '<div class="panel panel-body"><p>Submission history is available below, '
            "but Plotly is not installed in this renderer environment.</p></div>"
            if trajectory_payload is not None
            else '<div class="panel panel-body"><p>No scored submission trajectory was found.</p></div>'
        )
    )
    plotly_script = f"<script>{plotly_javascript}</script>" if plotly_javascript else ""

    run_id = str(run.get("run_id") or "unknown")
    task_id = str(run.get("task_id") or "unknown")
    timed_out = bool(run.get("timed_out"))
    termination_reason = str(
        run.get("termination_reason") or ("timeout" if timed_out else "completed")
    )
    outcome_status = str(result.get("outcome_status") or "unknown")
    observed_terminal_state = run.get("terminal_state")
    if isinstance(observed_terminal_state, str) and observed_terminal_state:
        terminal_state = observed_terminal_state
    elif timed_out:
        terminal_state = (
            "budget_reached"
            if termination_reason == "budget_exhausted"
            else "timed_out"
        )
    else:
        terminal_state = "complete"
    outcome_label = {
        "success": "Successful",
        "valid_result": "Valid result",
        "no_valid_result": "No valid result",
        "no_result": "No result",
    }.get(outcome_status, outcome_status.replace("_", " ").title())
    terminal_label = {
        "budget_reached": "Budget reached",
        "timed_out": "Timed out",
        "complete": "Completed",
        "completed": "Completed",
        "completed_in_finalization_grace": "Completed in finalization grace",
        "failed": "Failed",
    }.get(terminal_state, terminal_state.replace("_", " ").title())
    terminal_tone = (
        "success"
        if terminal_state == "budget_reached" and outcome_status == "success"
        else status_class(terminal_state)
    )
    total_submissions = int(
        result.get("total_rounds") or len(evaluations.get("entries") or [])
    )
    goal_plus = observation.get("goal_plus")
    goal_plus = goal_plus if isinstance(goal_plus, dict) else {}
    kpi_cards = [
        _metric_card(
            "Run status",
            terminal_label,
            f"{outcome_label} · resumes {numeric_value(run.get('resume_count'))}",
            terminal_tone,
        ),
        _metric_card(
            "Runtime",
            format_duration(run.get("runtime_seconds")),
            "observed agent runtime",
        ),
        _metric_card(
            "Model",
            format_text(run.get("model")),
            f"reasoning {format_text(run.get('reasoning_effort'))}",
        ),
        _metric_card(
            "Blocked Stop",
            numeric_value(stop.get("blocked")) if stop_observed else "Not observed",
            (
                f"{numeric_value(stop.get('blocked_per_agent_hour'), digits=1)} / agent-hour"
                if stop_observed
                else "no persisted hook evidence"
            ),
            "warning",
        ),
    ]
    if goal_plus.get("source_available"):
        goal_status = str(goal_plus.get("overall_status") or "unknown")
        kpi_cards.append(
            _metric_card(
                "Goal Plus finalization",
                {
                    "complete": "Complete",
                    "incomplete": "Incomplete",
                }.get(goal_status, goal_status.replace("_", " ").title()),
                f"{numeric_value(goal_plus.get('active_records'))} active record(s)",
                "success" if goal_status == "complete" else "failure",
            )
        )
    if total_submissions:
        kpi_cards.extend(
            [
                _metric_card(
                    "Best score",
                    numeric_value(result.get("best_score"), digits=4),
                    f"round {format_text(result.get('best_round'))}",
                    "success",
                ),
                _metric_card(
                    "Best pass rate",
                    format_percent(result.get("best_pass_rate")),
                    "persisted evaluator evidence",
                    "success",
                ),
                _metric_card(
                    "Submissions",
                    numeric_value(total_submissions),
                    f"{numeric_value(result.get('agent_submissions'))} agent / "
                    f"{numeric_value(result.get('auto_submissions'))} auto",
                ),
                _metric_card(
                    "Codex sessions",
                    numeric_value(len(run.get("session_ids") or [])),
                    ", ".join(run.get("codex_versions") or []) or "version unavailable",
                ),
            ]
        )
    else:
        kpi_cards.extend(
            [
                _metric_card(
                    "Processed tokens",
                    numeric_value(usage.get("processed_tokens")),
                    format_text(usage.get("scope")),
                ),
                _metric_card(
                    "API-equivalent cost",
                    format_cost(usage.get("cost_usd")),
                    "estimate when complete",
                ),
                _metric_card(
                    "Tool calls",
                    numeric_value(usage.get("tool_calls")),
                    f"{numeric_value(usage.get('assistant_messages'))} assistant messages",
                ),
                _metric_card(
                    "Codex sessions",
                    numeric_value(len(run.get("session_ids") or [])),
                    ", ".join(run.get("codex_versions") or []) or "version unavailable",
                ),
            ]
        )
    kpis = "".join(kpi_cards)
    warning_items = (
        "".join(f"<li>{escape_html(item)}</li>" for item in warnings)
        or "<li>No evidence warnings.</li>"
    )
    artifact_rows = "".join(
        "<tr>"
        f"<th>{escape_html(key.replace('_', ' ').title())}</th>"
        f"<td>{_artifact_link(value)}</td>"
        "</tr>"
        for key, value in evidence.items()
    )
    raw_payload = json.dumps(
        observation,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    )
    goal_plus_section = _render_codex_goal_plus_finalization(observation)
    goal_plus_nav = (
        '<a href="#goal-plus-finalization">Goal Plus</a>' if goal_plus_section else ""
    )
    print_icon = (
        '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 '
        '2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect width="12" height="8" x="6" y="14"/></svg>'
    )
    return f"""<!doctype html>
<html lang="en" data-report-schema="codex-observability-report/v{REPORT_DOCUMENT_SCHEMA_VERSION}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>Codex Execution Report: {escape(run_id)}</title>
  <script>document.documentElement.classList.add('js');</script>
  <style>{REPORT_CSS}</style>
</head>
<body data-run-id="{escape(run_id, quote=True)}">
  <header class="masthead">
    <div class="wrap masthead-inner">
      <div class="identity">
        <div class="eyebrow">Codex Execution Report</div>
        <div class="identity-line"><h1>{escape(run_id)}</h1><span class="status {status_class(terminal_state)}">{escape(terminal_label)}</span></div>
        <div class="id-line mono">Task: {escape(task_id)} · source: {escape_html(observation.get("source_kind"))}</div>
      </div>
      <div class="masthead-actions">
        <div class="generated">Generated<strong class="mono">{escape_html(observation.get("generated_at"))}</strong></div>
        <button class="button no-print" type="button" onclick="window.print()" title="Print report">{print_icon}<span>Print</span></button>
      </div>
    </div>
  </header>
  <nav class="section-nav no-print" aria-label="Report sections">
    <div class="wrap">
      <a href="#aggregate">Summary</a><a href="#execution">Execution</a>
      {goal_plus_nav}<a href="#trajectory">Trajectory</a><a href="#hooks">Stop hooks</a>
      <a href="#audit">Audit</a>
    </div>
  </nav>
  <main class="wrap">
    <section id="aggregate" class="report-section">
      <div class="section-kicker">Codex Run Summary</div>
      <div class="kpi-grid">{kpis}</div>
    </section>
    {goal_plus_section}
    <section id="execution" class="report-section">
      <div class="section-kicker">Host Execution Evidence</div>
      {_render_codex_execution_timeline(observation)}
      <div class="panel panel-body">
        <h3>Run Facts</h3>
        <dl class="fact-grid">
          <div class="fact"><dt>Agent</dt><dd>{escape_html(run.get("agent"))}</dd></div>
          <div class="fact"><dt>Model</dt><dd>{escape_html(run.get("model"))}</dd></div>
          <div class="fact"><dt>Reasoning effort</dt><dd>{escape_html(run.get("reasoning_effort"))}</dd></div>
          <div class="fact"><dt>Metric direction</dt><dd>{escape_html(evaluations.get("metric_direction"))}</dd></div>
          <div class="fact"><dt>Termination reason</dt><dd>{escape_html(termination_reason.replace("_", " ").title())}</dd></div>
          <div class="fact"><dt>Outcome</dt><dd>{escape_html(outcome_label)}</dd></div>
          <div class="fact"><dt>Raw timeout flag</dt><dd>{escape_html(str(timed_out).lower())}</dd></div>
          <div class="fact"><dt>Exploration budget</dt><dd>{escape_html(format_duration(run.get("exploration_budget_seconds")))}</dd></div>
          <div class="fact"><dt>Finalization grace</dt><dd>{escape_html(format_duration(run.get("finalization_grace_seconds")))}</dd></div>
          <div class="fact"><dt>Finalization runtime</dt><dd>{escape_html(format_duration(run.get("finalization_runtime_seconds")))}</dd></div>
        </dl>
      </div>
      <div class="panel panel-body">
        <h3>Usage Evidence</h3>
        <dl class="fact-grid">
          <div class="fact"><dt>Processed tokens</dt><dd>{escape_html(numeric_value(usage.get("processed_tokens")))}</dd></div>
          <div class="fact"><dt>Input tokens</dt><dd>{escape_html(numeric_value(usage.get("input_tokens")))}</dd></div>
          <div class="fact"><dt>Cached input tokens</dt><dd>{escape_html(numeric_value(usage.get("cached_input_tokens")))}</dd></div>
          <div class="fact"><dt>Output tokens</dt><dd>{escape_html(numeric_value(usage.get("output_tokens")))}</dd></div>
          <div class="fact"><dt>API-equivalent cost</dt><dd>{escape_html(format_cost(usage.get("cost_usd")))}</dd></div>
          <div class="fact"><dt>Tool calls</dt><dd>{escape_html(numeric_value(usage.get("tool_calls")))}</dd></div>
          <div class="fact"><dt>Assistant messages</dt><dd>{escape_html(numeric_value(usage.get("assistant_messages")))}</dd></div>
          <div class="fact"><dt>Usage scope</dt><dd>{escape_html(usage.get("scope"))}</dd></div>
        </dl>
      </div>
    </section>
    <section id="trajectory" class="report-section">
      <div class="section-kicker">Evaluator Evidence</div>
      {trajectory_html}
      <div class="panel panel-body">
        <h3>Submission Evidence</h3>
        {_render_codex_submissions(observation)}
      </div>
    </section>
    <section id="hooks" class="report-section">
      <div class="section-kicker">Codex Host Hook Evidence</div>
      <h2>Stop Hook Activity</h2>
      {hook_activity_html}
      <div class="subsection">
        <h3>Blocked Stop Distribution</h3>
        {stop_progress_html}
      </div>
      <p class="footnote">When a source preserves hook lifecycle counts and output positions but not per-hook timestamps, output-progress bins must not be interpreted as wall-clock time.</p>
    </section>
    <section id="audit" class="report-section">
      <h2>Report Audit</h2>
      <div class="two-column">
        <div class="panel panel-body"><h3>Evidence Warnings</h3><ul class="warning-list">{warning_items}</ul></div>
        <div class="panel panel-body"><h3>Evidence Availability</h3>
          <dl class="fact-grid">
            <div class="fact"><dt>Stop hook events</dt><dd>{escape_html(availability.get("stop_hook_events"))}</dd></div>
            <div class="fact"><dt>Per-event timestamps</dt><dd>{escape_html(availability.get("per_event_wall_clock_timestamps"))}</dd></div>
            <div class="fact"><dt>stop_hook_active</dt><dd>{escape_html(availability.get("stop_hook_active"))}</dd></div>
            <div class="fact"><dt>Token usage</dt><dd>{escape_html(availability.get("token_usage"))}</dd></div>
            <div class="fact"><dt>Goal Plus state</dt><dd>{escape_html(availability.get("goal_plus_state"))}</dd></div>
            <div class="fact"><dt>Reason</dt><dd>{escape_html(availability.get("reason"))}</dd></div>
          </dl>
        </div>
      </div>
      <div class="panel panel-body"><h3>Source Artifacts</h3>
        <div class="table-scroll"><table><tbody>{artifact_rows}</tbody></table></div>
      </div>
      <details class="summary-block"><summary>Complete normalized report data</summary><pre>{escape(raw_payload)}</pre></details>
      <p class="footnote">Schema codex-observability-report/v{REPORT_DOCUMENT_SCHEMA_VERSION}. This file is self-contained and contains normalized evidence only; prompts and assistant messages are excluded.</p>
    </section>
  </main>
  {plotly_script}
  <script>{REPORT_SCRIPT}</script>
</body>
</html>
"""


def render_report_document(
    document: dict[str, Any], *, plotly_javascript: str | None = None
) -> str:
    if not isinstance(document, dict):
        raise TypeError("report document must be a mapping")
    report_kind = document.get("report_kind")
    data = document.get("data")
    if not isinstance(data, dict):
        raise ValueError("report document data must be a mapping")
    if report_kind == "goal-plus":
        return render_html_report(data, plotly_javascript=plotly_javascript)
    if report_kind == "codex-run":
        return render_codex_observability_report(
            data, plotly_javascript=plotly_javascript
        )
    raise ValueError(f"unsupported report kind: {report_kind!r}")


def require_terminal_goal_plus_report(data: dict[str, Any]) -> None:
    goal_plus_id = data.get("goal_plus_id")
    if not goal_plus_id:
        return
    snapshot = data.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    goal = snapshot.get("goal_plus")
    goal = goal if isinstance(goal, dict) else {}
    status = str(goal.get("status") or "active")
    if (
        status in {"complete", "abandoned"}
        or (goal.get("control") or {}).get("state") == "closed"
        or ("control" not in goal and status == "blocked")
    ):
        return
    raise RuntimeError(
        "cannot generate a Goal Plus HTML report before the linked record "
        "reaches a terminal status (complete or abandoned); "
        f"current: {goal_plus_id}={status}"
    )
