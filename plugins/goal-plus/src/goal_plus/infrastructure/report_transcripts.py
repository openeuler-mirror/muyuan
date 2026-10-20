"""Optional navigation to a derived local transcript export; never execution state."""

from pathlib import Path
from typing import Any

from goal_plus.infrastructure.api_debug import api_debug_enabled
from goal_plus.infrastructure.state.files import load_json


def attach_transcript_links(root: Path, task: dict[str, Any]) -> None:
    directory = root / "host-logs/agent-transcript"
    page = directory / "index.html"
    try:
        manifest = load_json(directory / "manifest.json") if page.is_file() else None
    except (OSError, ValueError, TypeError):
        manifest = None
    manifest_present = isinstance(manifest, dict) and manifest.get("schema_version") == 1 and isinstance(manifest.get("sessions"), list)
    records = manifest["sessions"] if manifest_present else []
    records = [record for record in records if isinstance(record, dict)
               and isinstance(record.get("identity"), dict) and isinstance(record.get("anchor"), str)]
    run_id = task.get("run_id")

    def summary(candidate_id: str, session_id: str | None = None, iteration: dict | None = None) -> dict:
        matching = [record for record in records if record["identity"].get("run_id") == run_id
                    and record["identity"].get("candidate_id") == candidate_id
                    and (session_id is None or record["identity"].get("agent_session_id") == session_id
                         or record["identity"].get("worker_session_id") == session_id)]
        if iteration is not None:
            matching = [record for record in matching if record.get("role") not in {"verifier", "annotator"} or (
                record["identity"].get("iteration") == iteration.get("iteration")
                and iteration.get("settlement_id") is not None
                and record["identity"].get("settlement_id") == iteration.get("settlement_id")
            )]
        links = []
        exact = False
        for record in matching:
            context = record.get("report_context") or {}
            anchor = record["anchor"]
            if iteration is not None:
                matches = [item for item in context.get("iterations", [])
                           if item.get("iteration") == iteration.get("iteration")
                           and item.get("settlement_id") == iteration.get("settlement_id")
                           and item.get("event_sequences")]
                if len(matches) == 1:
                    anchor = matches[0].get("event_anchor") or anchor + f'-e{matches[0]["event_sequences"][0]}'
                    exact = True
                elif record.get("role") in {"verifier", "annotator"}:
                    exact = True
            collection = context.get("collection") or {}
            links.append({"href": "../../host-logs/agent-transcript/index.html#" + anchor,
                          "role": record.get("role"), "session_id": record.get("session_id"),
                          "state": collection.get("state") or ("controller_only" if record.get("role") == "controller-journal"
                              else "partial" if record.get("warnings") else "readable_history"),
                          "reason": collection.get("reason"), "warnings": record.get("warnings") or []})
        body = [link for link in links if link["state"] != "controller_only"]
        no_readable_history = bool(body) and all(link["state"] in {"unavailable", "parse_failed"} for link in body)
        state = (("parse_failed" if any(link["state"] == "parse_failed" for link in body) else "unavailable")
                 if no_readable_history else
                 "partial" if any(link["state"] in {"partial", "unavailable", "parse_failed"} for link in body)
                 else "available" if body else "controller_only" if links else
                 "not_collected" if manifest_present else "not_exported")
        reason = ("No readable history in the matching sources; see each source's collection reason." if no_readable_history else
                  "Persistent records only; actual provider window is not established." if body else
                  "Only controller requests/results are available; Child history is unavailable." if links else
                  "No matching history in the export; the source may be absent or was not collected." if manifest_present else
                  "No usable transcript manifest. Manual export can read any retained native history. "
                  + ("DEBUG collection is enabled now; its historical setting is unknown." if api_debug_enabled()
                     else "DEBUG collection is disabled now; its historical setting is unknown."))
        return {"state": state, "reason": reason, "event_state": "exact" if exact else "unknown", "links": links}

    for candidate in task.get("candidates") or []:
        candidate_id = candidate["candidate_id"]
        candidate["report_anchor"] = f"candidate-{run_id}-{candidate_id}"
        candidate["transcript"] = summary(candidate_id)
        for iteration in candidate.get("iterations") or []:
            iteration["report_anchor"] = f'iteration-{run_id}-{candidate_id}-{iteration["iteration"]}'
            iteration["transcript"] = summary(candidate_id, iteration.get("agent_session_id"), iteration)
    for session in task.get("sessions") or []:
        session["report_anchor"] = f'worker-{run_id}-{session["agent_session_id"]}'
        session["transcript"] = summary(session["candidate_id"], session["agent_session_id"])
