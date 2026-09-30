"""Read-only export from candidate trace, domain state, and native history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.infrastructure.agent_transcript import collect_runtime_transcripts
from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.infrastructure.state.candidate_trace import FileCandidateTraceStore
from goal_plus.infrastructure.state.files import write_json, write_text
from goal_plus.projections.agent_transcript import AgentTranscriptSession
from goal_plus.projections.candidate_trace import render_candidate_trace_markdown


_EVIDENCE_TOOLS = {
    "evidence.global_returned": {
        "explicit": ("goal_plus_search_get_global_evidence",),
        "verifier_injected": (
            "goal_plus_search_run_verifier",
            "goal_plus_search_get_agent_context",
        ),
    },
    "evidence.detail_returned": {
        "explicit": ("goal_plus_search_get_evidence_detail",),
    },
}


def _warning(code: str, **details: Any) -> dict[str, Any]:
    return {"code": code, **details}


def _decoded_values(text: str) -> Iterable[Any]:
    try:
        value: Any = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    values: list[Any] = []
    pending = [value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        values.append(current)
        if isinstance(current, str):
            try:
                pending.append(json.loads(current))
            except json.JSONDecodeError:
                pass
        elif isinstance(current, dict):
            for key in (
                "global_evidence_snapshot",
                "result",
                "structuredContent",
                "content",
                "text",
            ):
                if key in current:
                    pending.append(current[key])
        elif isinstance(current, list):
            for item in current:
                if isinstance(item, dict) and item.get("type") == "text":
                    pending.append(item.get("text"))
    return values


def _tool_name_matches(label: str, expected: tuple[str, ...]) -> bool:
    return any(label == name or label.endswith(f"__{name}") for name in expected)


def _candidate_transcripts(
    transcripts: list[AgentTranscriptSession],
    *,
    run_id: str,
    candidate_id: str,
    agent_session_id: str,
) -> list[AgentTranscriptSession]:
    return [
        session
        for session in transcripts
        if session.identity.get("agent_session_id") == agent_session_id
        and session.identity.get("run_id", run_id) == run_id
        and session.identity.get("candidate_id", candidate_id) == candidate_id
        and session.role in {"candidate", "tt-child"}
    ]


def _delivery(
    event: dict[str, Any],
    snapshot_content: Any | None,
    transcripts: list[AgentTranscriptSession],
) -> dict[str, Any]:
    if snapshot_content is None:
        return {"state": "unknown", "reason": "snapshot_unavailable"}
    sessions = _candidate_transcripts(
        transcripts,
        run_id=event["run_id"],
        candidate_id=event["candidate_id"],
        agent_session_id=event["agent_session_id"],
    )
    if not sessions:
        return {"state": "unknown", "reason": "native_history_unavailable"}
    source = event["payload"].get("source", "explicit")
    expected = _EVIDENCE_TOOLS[event["kind"]].get(source, ())
    host_call_id = event.get("host_call_id")
    matches: list[dict[str, Any]] = []
    for session in sessions:
        for index, native_event in enumerate(session.events, 1):
            if (
                native_event.kind != "tool_result"
                or not _tool_name_matches(native_event.label, expected)
                or (
                    host_call_id is not None
                    and native_event.meta.get("call_id") != host_call_id
                )
            ):
                continue
            if any(
                sha256_text(canonical_json(value)) == event["payload"]["snapshot_id"]
                for value in _decoded_values(native_event.content)
            ):
                matches.append(
                    {
                        "source": session.source_path,
                        "event_index": index,
                        "occurred_at": native_event.at,
                        "call_id": native_event.meta.get("call_id"),
                    }
                )
    if len(matches) == 1:
        return {"state": "observed", "transcript_locator": matches[0]}
    return {
        "state": "unknown",
        "reason": "native_result_not_found" if not matches else "native_result_ambiguous",
        "matching_results": len(matches),
    }


class CandidateTraceExporter:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.state = FileCandidateQueries(self.root_dir)
        self.archive = FileCandidateTraceStore(self.root_dir)

    def export(
        self,
        run_id: str,
        *,
        candidate_id: str | None = None,
        output: Path | str | None = None,
    ) -> dict[str, Any]:
        self.state.read_run(run_id)
        candidates = self.state.read_candidates(run_id)
        if candidate_id is not None:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.candidate_id == candidate_id
            ]
            if not candidates:
                raise ValueError(f"candidate not found in run: {candidate_id}")
        transcripts = collect_runtime_transcripts(self.root_dir)
        base = (
            Path(output).expanduser().resolve()
            if output is not None
            else self.root_dir / "host-logs" / "candidate-traces" / run_id
        )
        written = []
        for candidate in candidates:
            document = self._candidate_document(run_id, candidate, transcripts)
            destination = base / candidate.candidate_id
            write_json(destination / "trace.json", document)
            write_text(
                destination / "trace.md",
                render_candidate_trace_markdown(document),
            )
            written.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "directory": str(destination),
                    "completeness": document["completeness"]["status"],
                }
            )
        return {"run_id": run_id, "output": str(base), "candidates": written}

    def _candidate_document(
        self,
        run_id: str,
        candidate: Any,
        transcripts: list[AgentTranscriptSession],
    ) -> dict[str, Any]:
        candidate_id = candidate.candidate_id
        audited = self.archive.audit_events(run_id, candidate_id)
        warnings = list(audited.warnings)
        sessions = [
            session
            for session in self.state.read_agent_sessions(run_id)
            if session.candidate_id == candidate_id
        ]
        sessions.sort(key=lambda item: (item.created_at, item.agent_session_id))
        sessions_by_id = {item.agent_session_id: item for item in sessions}
        event_documents: list[dict[str, Any]] = []
        snapshot_contents: dict[str, Any] = {}
        snapshots: dict[str, dict[str, Any]] = {}

        for event in audited.events:
            raw = event.model_dump(mode="json", exclude_none=True)
            session = sessions_by_id.get(event.agent_session_id)
            if session is None or session.execution_generation != event.execution_generation:
                warnings.append(
                    _warning(
                        "event_session_identity_mismatch",
                        event_id=event.event_id,
                        agent_session_id=event.agent_session_id,
                    )
                )
            snapshot_id = event.payload.get("snapshot_id")
            if isinstance(snapshot_id, str):
                source = (
                    self.archive.candidate_dir(run_id, candidate_id)
                    / "snapshots"
                    / f"{snapshot_id}.json"
                )
                try:
                    snapshot = self.archive.read_snapshot(
                        run_id, candidate_id, snapshot_id
                    )
                    snapshot_contents[snapshot_id] = snapshot.content
                    snapshots[snapshot_id] = {
                        "snapshot_id": snapshot_id,
                        "source": str(source),
                        "bytes": len(canonical_json(snapshot.content).encode("utf-8")),
                        "verified": True,
                    }
                except (OSError, ValueError, TypeError) as exc:
                    warnings.append(
                        _warning(
                            "snapshot_unavailable",
                            event_id=event.event_id,
                            snapshot_id=snapshot_id,
                            source=str(source),
                            error_category=type(exc).__name__,
                        )
                    )
                    snapshots[snapshot_id] = {
                        "snapshot_id": snapshot_id,
                        "source": str(source),
                        "bytes": 0,
                        "verified": False,
                    }
            if event.kind in _EVIDENCE_TOOLS:
                raw["delivery"] = _delivery(
                    raw,
                    snapshot_contents.get(str(snapshot_id)),
                    transcripts,
                )
                if raw["delivery"]["state"] != "observed":
                    warnings.append(
                        _warning(
                            "delivery_unknown",
                            event_id=event.event_id,
                            reason=raw["delivery"]["reason"],
                        )
                    )
            raw["source"] = "business_trace"
            event_documents.append(raw)

        self._check_receipts(sessions, event_documents, warnings)
        self._check_references(run_id, event_documents, warnings)
        derived = self._settlement_projection(candidate, event_documents, warnings)
        boundaries = []
        for session in sessions:
            native = _candidate_transcripts(
                transcripts,
                run_id=run_id,
                candidate_id=candidate_id,
                agent_session_id=session.agent_session_id,
            )
            if not native:
                history_status = "unavailable"
                sources: list[str] = []
                warnings.append(
                    _warning(
                        "native_history_unavailable",
                        agent_session_id=session.agent_session_id,
                    )
                )
            elif any(item.warnings for item in native):
                history_status = "partial"
                sources = [item.source_path for item in native]
                warnings.append(
                    _warning(
                        "native_history_partial",
                        agent_session_id=session.agent_session_id,
                        history_warnings=[
                            warning
                            for item in native
                            for warning in item.warnings
                        ],
                    )
                )
            else:
                history_status = "observed"
                sources = [item.source_path for item in native]
            boundaries.append(
                {
                    "source": "agent_session_record",
                    "agent_session_id": session.agent_session_id,
                    "execution_generation": session.execution_generation,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                    "history_status": history_status,
                    "history_sources": sources,
                }
            )

        has_activity = bool(
            candidate.iterations
            or any(
                session.global_evidence_reads or session.evidence_detail_reads
                for session in sessions
            )
        )
        if audited.raw_event_count == 0 and has_activity:
            status = "legacy_unavailable"
            warnings.append(_warning("legacy_trace_unavailable"))
        elif warnings:
            status = "partial"
        else:
            status = "complete"
        return {
            "schema_version": 1,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "events": event_documents,
            "derived_events": derived,
            "snapshots": list(snapshots.values()),
            "session_boundaries": boundaries,
            "completeness": {"status": status, "warnings": warnings},
        }

    @staticmethod
    def _check_receipts(
        sessions: list[Any],
        events: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> None:
        event_by_key = {
            (event["kind"], event["operation_id"]): event for event in events
        }
        receipt_keys: set[tuple[str, str]] = set()
        for session in sessions:
            for receipt in session.global_evidence_reads:
                if receipt.operation_id is None:
                    warnings.append(
                        _warning(
                            "legacy_global_receipt_unlinked",
                            agent_session_id=session.agent_session_id,
                            read_at=receipt.read_at,
                        )
                    )
                    continue
                key = ("evidence.global_returned", receipt.operation_id)
                receipt_keys.add(key)
                event = event_by_key.get(key)
                if event is None:
                    warnings.append(_warning("global_event_missing", operation_id=receipt.operation_id))
                elif event["payload"]["snapshot_id"] != receipt.snapshot_id:
                    warnings.append(_warning("global_receipt_mismatch", operation_id=receipt.operation_id))
            for receipt in session.evidence_detail_reads:
                if receipt.operation_id is None:
                    warnings.append(
                        _warning(
                            "legacy_detail_receipt_unlinked",
                            agent_session_id=session.agent_session_id,
                            read_at=receipt.read_at,
                        )
                    )
                    continue
                key = ("evidence.detail_returned", receipt.operation_id)
                receipt_keys.add(key)
                event = event_by_key.get(key)
                if event is None:
                    warnings.append(_warning("detail_event_missing", operation_id=receipt.operation_id))
                elif event["payload"]["snapshot_id"] != receipt.snapshot_id:
                    warnings.append(_warning("detail_receipt_mismatch", operation_id=receipt.operation_id))
        for key, event in event_by_key.items():
            if key[0].startswith("evidence.") and key not in receipt_keys:
                warnings.append(
                    _warning(
                        "evidence_receipt_missing",
                        event_id=event["event_id"],
                        operation_id=event["operation_id"],
                    )
                )

    def _check_references(
        self,
        run_id: str,
        events: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> None:
        candidates = {
            record.candidate_id: record for record in self.state.read_candidates(run_id)
        }
        for event in events:
            if event["kind"] != "reference.opened":
                continue
            payload = event["payload"]
            source = candidates.get(payload["source_candidate_id"])
            iteration = next(
                (
                    item
                    for item in source.iterations
                    if item.iteration == payload["source_iteration"]
                ),
                None,
            ) if source is not None else None
            if (
                iteration is None
                or iteration.artifact_ref is None
                or iteration.artifact_ref.model_dump(mode="json")
                != payload["artifact_ref"]
            ):
                warnings.append(
                    _warning(
                        "reference_artifact_mismatch",
                        event_id=event["event_id"],
                    )
                )

    @staticmethod
    def _settlement_projection(
        candidate: Any,
        events: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        submitted = {
            event["operation_id"]
            for event in events
            if event["kind"] == "verifier.submitted"
        }
        settled = {
            event["operation_id"]
            for event in events
            if event["kind"] == "verifier.settled"
        }
        derived = []
        for iteration in candidate.iterations:
            receipt = iteration.settlement_receipt
            attempt = iteration.metrics.get("_candidate_attempt")
            attempt_id = attempt.get("id") if isinstance(attempt, dict) else None
            if receipt is None or not isinstance(attempt_id, str):
                continue
            if attempt_id not in submitted and iteration.agent_session_id is not None:
                warnings.append(
                    _warning("verifier_submission_missing", attempt_id=attempt_id)
                )
            if attempt_id in settled:
                continue
            warnings.append(
                _warning(
                    "verifier_settlement_event_missing",
                    attempt_id=attempt_id,
                    settlement_id=receipt.settlement_id,
                )
            )
            derived.append(
                {
                    "source": "derived_from_settlement",
                    "kind": "verifier.settled",
                    "operation_id": attempt_id,
                    "occurred_at": receipt.created_at,
                    "agent_session_id": iteration.agent_session_id,
                    "payload": {
                        "attempt_id": attempt_id,
                        "settlement_id": receipt.settlement_id,
                        "iteration": receipt.iteration,
                        "attempt_base_artifact_ref": (
                            receipt.attempt_base_artifact_ref.model_dump(mode="json")
                            if receipt.attempt_base_artifact_ref is not None
                            else None
                        ),
                        "attempt_artifact_ref": receipt.attempt_artifact_ref.model_dump(mode="json"),
                        "post_settlement_artifact_ref": receipt.post_settlement_artifact_ref.model_dump(mode="json"),
                        "attempt_changed_files": receipt.evidence.attempt_changed_files,
                        "process_passed": receipt.evidence.process_passed,
                        "score": receipt.evidence.hard_score,
                        "failure_class": receipt.evidence.failure_class,
                        "disposition": receipt.disposition,
                        "settled_at": receipt.created_at,
                    },
                }
            )
        return derived
