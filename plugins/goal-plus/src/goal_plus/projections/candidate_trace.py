"""Human-readable rendering for candidate execution trace exports."""

from __future__ import annotations

import json
from typing import Any


def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _cell(value: Any) -> str:
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def render_candidate_trace_markdown(document: dict[str, Any]) -> str:
    completeness = document["completeness"]
    lines = [
        f"# Candidate trace: {document['candidate_id']}",
        "",
        f"- Run: `{document['run_id']}`",
        f"- Completeness: `{completeness['status']}`",
        f"- Raw events: {len(document['events'])}",
        f"- Derived events: {len(document['derived_events'])}",
        "",
        "## Sessions",
        "",
        "| Session | Generation | Created | Updated | Native history |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for boundary in document["session_boundaries"]:
        lines.append(
            "| "
            + " | ".join(
                _cell(boundary.get(key))
                for key in (
                    "agent_session_id",
                    "execution_generation",
                    "created_at",
                    "updated_at",
                    "history_status",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Business Events",
            "",
            "| Seq | Time | Kind | Operation | Summary | Delivery |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for event in document["events"]:
        payload = event["payload"]
        summary = {
            key: payload[key]
            for key in (
                "source",
                "target_candidate_id",
                "target_iteration",
                "source_candidate_id",
                "source_iteration",
                "attempt_id",
                "status",
                "iteration",
                "score",
                "process_passed",
                "disposition",
                "error_category",
            )
            if key in payload
        }
        delivery = event.get("delivery", {}).get("state", "-")
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    event["sequence"],
                    event["occurred_at"],
                    event["kind"],
                    event["operation_id"],
                    _compact(summary),
                    delivery,
                )
            )
            + " |"
        )
        locator = event.get("delivery", {}).get("transcript_locator")
        if locator:
            lines.append(
                f"  - Native transcript: `{locator['source']}` event {locator['event_index']}"
            )
    if document["derived_events"]:
        lines.extend(["", "## Derived Settlement Events", ""])
        for event in document["derived_events"]:
            lines.append(
                f"- `{event['occurred_at']}` `{event['kind']}` "
                f"attempt `{event['operation_id']}` from `{event['source']}`"
            )
    lines.extend(["", "## Snapshots", ""])
    if document["snapshots"]:
        for snapshot in document["snapshots"]:
            lines.append(
                f"- `{snapshot['snapshot_id']}`: `{snapshot['source']}` "
                f"({snapshot['bytes']} bytes, verified={str(snapshot['verified']).lower()})"
            )
    else:
        lines.append("No returned Evidence snapshots were recorded.")
    lines.extend(["", "## Warnings", ""])
    if completeness["warnings"]:
        for warning in completeness["warnings"]:
            detail = _compact({key: value for key, value in warning.items() if key != "code"})
            lines.append(f"- `{warning['code']}` {detail if detail != '{}' else ''}".rstrip())
    else:
        lines.append("No consistency warnings.")
    lines.append("")
    return "\n".join(lines)
