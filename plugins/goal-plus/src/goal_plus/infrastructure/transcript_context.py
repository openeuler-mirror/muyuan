"""Read-only links from native histories to existing Search identities."""

from dataclasses import replace
import json
import logging
from pathlib import Path

from goal_plus.application.candidate_host import ATTEMPT_FEEDBACK_HEADER
from goal_plus.domain.evidence import CandidateAttemptFeedback, IterationRecord
from goal_plus.infrastructure.state.search_run import FileSearchRunState
from goal_plus.projections.agent_transcript import AgentTranscriptSession, transcript_anchor, transcript_settlement_anchor

logger = logging.getLogger(__name__)


def _checkpoint_sequence(session: AgentTranscriptSession, binding: dict) -> int | None:
    checkpoint = binding["checkpoint_id"]
    matches = [index for index, event in enumerate(session.events, 1) if (
        event.meta.get("source_entry_id") == checkpoint
        if binding["checkpoint_kind"] == "entry" else
        event.label == "task complete" and event.meta.get("native_turn_id") == checkpoint
    )]
    return max(matches) if matches else None


def _settlement_sequences(session: AgentTranscriptSession, run_id: str, candidate_id: str,
                          iteration: IterationRecord) -> list[int]:
    if not iteration.settlement_id:
        return []
    sequences = []
    for sequence, event in enumerate(session.events, 1):
        try:
            if event.kind == "user" and session.identity.get("thinkthread_id"):
                marker = "\n\n" + ATTEMPT_FEEDBACK_HEADER + "\n"
                if event.content.count(marker) != 1:
                    continue
                # The existing continuation template carries one JSON line;
                # do not search arbitrary prose or infer feedback by timing.
                encoded = event.content.partition(marker)[2].partition("\n")[0]
                feedback = CandidateAttemptFeedback.model_validate_json(encoded)
                if not (
                    feedback.agent_session_id == iteration.agent_session_id
                    and feedback.agent_session_id == session.identity.get("agent_session_id")
                ):
                    continue
                payload = feedback.score_report.model_dump(mode="json")
            elif event.kind == "tool_result" and event.label.endswith(("goal_plus_search_run_verifier", "search_run_verifier")):
                payload = json.loads(event.content)
                if isinstance(payload, dict):
                    if payload.get("isError") is True:
                        continue
                    if not isinstance(payload.get("structuredContent"), dict) and "content" in payload:
                        content = payload["content"]
                        if not (
                            isinstance(content, list) and len(content) == 1
                            and isinstance(content[0], dict) and content[0].get("type") == "text"
                            and isinstance(content[0].get("text"), str)
                        ):
                            continue
                        payload = json.loads(content[0]["text"])
            else:
                continue
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("score_report"), dict):
            payload = payload["score_report"]
        elif isinstance(payload.get("structuredContent"), dict):
            payload = payload["structuredContent"]
        receipt = payload.get("settlement_receipt") or payload
        same_owner = isinstance(receipt, dict) and (
            receipt.get("run_id") == run_id
            and receipt.get("candidate_id") == candidate_id
        )
        exact_settlement = same_owner and receipt.get("settlement_id") == iteration.settlement_id and receipt.get("iteration") == iteration.iteration
        attempt = iteration.metrics.get("_candidate_attempt") or {}
        exact_attempt = same_owner and (
            attempt.get("id") is not None and receipt.get("attempt_id") == attempt["id"]
            and receipt.get("attempt_iteration") == iteration.iteration
            and receipt.get("attempt_artifact_ref") == (iteration.artifact_ref.model_dump(mode="json") if iteration.artifact_ref else None)
            and receipt.get("frozen_spec_id") == iteration.frozen_spec_id
            and receipt.get("spec_hash") == iteration.spec_hash
        )
        if exact_settlement or exact_attempt:
            sequences.append(sequence)
    return sequences


def attach_report_context(root: Path, sessions: list[AgentTranscriptSession]) -> None:
    """Attach only verified records; unavailable/ambiguous joins remain unknown.

    No timestamp attribution or latest active-contract backfill is used here.
    Reading broken historical state must not prevent exporting readable text.
    """
    state = FileSearchRunState(root)
    for run_path in sorted((root / "runs").glob("*/run.json")):
        try:
            run = state.read_run(run_path.parent.name)
            candidates = {c.candidate_id: c for c in state.read_candidates(run.run_id)}
            records = state.read_agent_sessions(run.run_id)
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("transcript Search links unavailable: %s", type(exc).__name__)
            continue
        for record in records:
            if not any((session.identity.get("run_id") == run.run_id
                        and session.identity.get("agent_session_id") == record.agent_session_id
                        and session.role not in {"verifier", "annotator", "controller-journal"})
                       or (session.role not in {"verifier", "annotator", "controller-journal"}
                           and record.session_handle.external_id is not None
                           and record.session_handle.external_id in (session.session_id, session.identity.get("thinkthread_id")))
                       for session in sessions):
                sessions.append(AgentTranscriptSession(
                    session_id=record.session_handle.external_id or record.agent_session_id,
                    harness=record.agent_harness, role="candidate",
                    identity={"run_id": run.run_id, "candidate_id": record.candidate_id,
                              "agent_session_id": record.agent_session_id},
                    report_context={"collection": {"state": "unavailable", "reason":
                        "No readable native history or retained archive found; prior collection state is unknown."}},
                ))
        for session in sessions:
            identity = session.identity
            if session.role in {"verifier", "annotator"}:
                if identity.get("run_id") != run.run_id:
                    continue
                candidate = candidates.get(identity.get("candidate_id"))
                matching_iterations = [item for item in candidate.iterations if
                    item.iteration == identity.get("iteration") and item.settlement_id is not None
                    and item.settlement_id == identity.get("settlement_id")
                    and (identity.get("worker_session_id") is None
                         or identity["worker_session_id"] == item.agent_session_id)
                ] if candidate else []
                if len(matching_iterations) != 1:
                    continue
                item = matching_iterations[0]
                session.report_context.update({
                    "report_href": f"../../runs/{run.run_id}/report.html",
                    "candidate_anchor": f"candidate-{run.run_id}-{candidate.candidate_id}",
                    "session_anchor": f"worker-{run.run_id}-{item.agent_session_id}" if item.agent_session_id else "",
                    "iterations": [{"iteration": item.iteration, "frozen_spec_id": item.frozen_spec_id or run.frozen_spec_id,
                        "spec_hash": item.spec_hash, "settlement_id": item.settlement_id,
                        "anchor": f"iteration-{run.run_id}-{candidate.candidate_id}-{item.iteration}",
                        "event_sequences": []}],
                })
                if item.agent_session_id:
                    session.identity = {**identity, "worker_session_id": item.agent_session_id}
                continue
            matching = [record for record in records if (
                identity.get("run_id") == run.run_id
                and identity.get("agent_session_id") == record.agent_session_id
                and identity.get("candidate_id") in (None, record.candidate_id)
            ) or (
                session.role not in {"controller-journal", "verifier", "annotator"}
                and record.session_handle.external_id is not None
                and record.session_handle.external_id in
                (session.session_id, identity.get("thinkthread_id"))
                and identity.get("run_id") in (None, run.run_id)
                and identity.get("candidate_id") in (None, record.candidate_id)
            )]
            if len(matching) != 1:
                continue
            record = matching[0]
            candidate = candidates.get(record.candidate_id)
            if candidate is None:
                continue
            session.identity = {**identity, "run_id": run.run_id,
                                "candidate_id": record.candidate_id,
                                "agent_session_id": record.agent_session_id}
            if session.role == "tt-child":
                session.role = "candidate"
            context = session.report_context
            context["mode"] = "fork" if record.candidate_origin is not None else "fresh"
            context["report_href"] = f"../../runs/{run.run_id}/report.html"
            context["session_anchor"] = f"worker-{run.run_id}-{record.agent_session_id}"
            context["candidate_anchor"] = f"candidate-{run.run_id}-{record.candidate_id}"
            context["iterations"] = [{
                "iteration": item.iteration,
                "frozen_spec_id": item.frozen_spec_id or run.frozen_spec_id,
                "spec_hash": item.spec_hash,
                "settlement_id": item.settlement_id,
                "anchor": f"iteration-{run.run_id}-{record.candidate_id}-{item.iteration}",
                "event_sequences": _settlement_sequences(session, run.run_id,
                    record.candidate_id, item),
            } for item in candidate.iterations if item.agent_session_id == record.agent_session_id]
            if record.candidate_origin is not None:
                binding = record.candidate_origin.model_dump(mode="json")
                context["candidate_origin"] = binding
                context["inherited_events"] = _checkpoint_sequence(session, binding)
                parents = [parent for parent in sessions if parent is not session and parent.role in {"main", "codex"} and
                           parent.session_id in {binding["parent_native_session_id"], binding.get("main_session_id")}]
                if len(parents) == 1:
                    parent = parents[0]
                    sequence = _checkpoint_sequence(parent, binding)
                    context["parent_anchor"] = transcript_anchor(parent) + (f"-e{sequence}" if sequence else "")
            # Event labels are derived from an exact settlement, never from time proximity.
            by_sequence = {sequence: item for item in context["iterations"] for sequence in item["event_sequences"]}
            for item in context["iterations"]:
                if item["event_sequences"]:
                    item["event_anchor"] = transcript_settlement_anchor(session, item["settlement_id"])
            session.events = [replace(event, meta={**event.meta,
                "iteration": by_sequence[index]["iteration"],
                "frozen_spec_id": by_sequence[index]["frozen_spec_id"],
                "settlement_id": by_sequence[index]["settlement_id"],
                **({"settlement_anchor": by_sequence[index]["event_anchor"]}
                   if index == by_sequence[index]["event_sequences"][0] else {}),
            }) if index in by_sequence else event for index, event in enumerate(session.events, 1)]
