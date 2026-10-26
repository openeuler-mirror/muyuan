"""Bounded persisted research queries used by successor runs and history."""

from typing import Any
from goal_plus.application.candidate_ranking import (
    record_ranking_score,
    top_records,
    records_by_created,
    best_iteration_record,
)
from goal_plus.application.candidate_spec import candidate_spec_id
from goal_plus.domain.search import AgentSessionRecord
from goal_plus.domain.evidence import public_metric_projection
from goal_plus.ports.state import CandidateRecord
from goal_plus.ports.candidate_host import CandidateHostSource


class SearchResearch:
    def __init__(self, source: CandidateHostSource) -> None:
        self.source = source

    def display_session_handle(self, session: AgentSessionRecord) -> str:
        return (
            session.session_handle.external_id
            or session.session_handle.nickname
            or ""
        )

    def candidate_research_summary(
        self,
        run_id: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        """Return the latest bounded worker-authored research handoff.

        This is deliberately a compact cross-round summary, not a transcript
        or a generalized experience-memory layer. Older handoff keys remain
        readable so existing runs still contribute useful evidence.
        """

        def items(value: Any) -> list[Any]:
            if value is None or value == "":
                return []
            if isinstance(value, list):
                return value[:5]
            return [value]

        for session in reversed(self.source.read_agent_sessions(run_id)):
            if session.candidate_id != candidate_id:
                continue
            metadata = session.session_handle.metadata or {}
            progress = metadata.get("progress_handoff")
            if not isinstance(progress, dict):
                continue
            model_handoff = progress.get("model_handoff")
            if not isinstance(model_handoff, dict):
                legacy_keys = {
                    "key_results",
                    "what_was_tried",
                    "pitfalls",
                    "blockers",
                    "next_steps",
                    "next_action",
                    "verifier_assessment",
                }
                if legacy_keys.intersection(progress):
                    model_handoff = progress
                else:
                    continue
            summary = model_handoff.get("summary")
            if not isinstance(summary, str):
                summary = progress.get("summary")
            key_results = items(
                model_handoff.get("key_results", model_handoff.get("what_was_tried"))
            )
            verifier_assessment = model_handoff.get("verifier_assessment")
            if not isinstance(verifier_assessment, dict):
                verifier_assessment = {
                    "status": "unknown",
                    "evidence": [],
                    "impact": "",
                    "recommended_action": "keep_spec",
                }
            else:
                verifier_assessment = dict(verifier_assessment)
                status = verifier_assessment.get("status")
                evidence = items(verifier_assessment.get("evidence"))
                if status not in {"adequate", "concern", "unknown"}:
                    status = "unknown"
                if status == "concern" and not evidence:
                    status = "unknown"
                action = verifier_assessment.get("recommended_action")
                if action not in {"keep_spec", "investigate", "upgrade_spec"}:
                    action = "investigate" if status == "concern" else "keep_spec"
                verifier_assessment = {
                    "status": status,
                    "evidence": evidence,
                    "impact": str(verifier_assessment.get("impact") or ""),
                    "recommended_action": action,
                }
            return {
                "summary": summary if isinstance(summary, str) else "",
                "key_results": key_results,
                "feature_ledger": key_results,
                "pitfalls": items(model_handoff.get("pitfalls")),
                "blockers": items(model_handoff.get("blockers")),
                "next_steps": items(
                    model_handoff.get("next_steps", model_handoff.get("next_action"))
                ),
                "verifier_assessment": verifier_assessment,
                "source_agent_session_id": session.agent_session_id,
            }
        return {
            "summary": "",
            "key_results": [],
            "feature_ledger": [],
            "pitfalls": [],
            "blockers": [],
            "next_steps": [],
            "verifier_assessment": {
                "status": "unknown",
                "evidence": [],
                "impact": "",
                "recommended_action": "keep_spec",
            },
            "source_agent_session_id": None,
        }

    def run_research_rollup(
        self,
        records: list[CandidateRecord],
        *,
        visible_candidate_ids: list[str],
    ) -> dict[str, Any]:
        """Keep portable discoveries visible even when candidates leave the frontier."""

        visible = set(visible_candidate_ids)
        feature_groups: list[list[dict[str, Any]]] = []
        verifier_assessments: list[dict[str, Any]] = []
        pitfall_groups: list[list[dict[str, Any]]] = []

        for record in records_by_created(records):
            run = self.source.read_run(record.task.run_id)
            frozen = self.source.read_frozen_spec(candidate_spec_id(run, record))
            research = self.candidate_research_summary(
                record.task.run_id,
                record.candidate_id,
            )
            best_iteration = best_iteration_record(record, frozen.spec.metric_direction)
            score = record_ranking_score(record, frozen.spec)
            best_git_head = best_iteration.git_head if best_iteration else None

            candidate_features: list[dict[str, Any]] = []
            for result in research["feature_ledger"]:
                item = (
                    dict(result)
                    if isinstance(result, dict)
                    else {"conclusion": str(result)}
                )
                candidate_features.append(
                    {
                        **item,
                        "candidate_id": record.candidate_id,
                        "candidate_visible": record.candidate_id in visible,
                        "candidate_score": score,
                        "frozen_spec_id": frozen.frozen_spec_id,
                        "spec_hash": frozen.spec_hash,
                        "metric_name": frozen.spec.metric_name,
                        "metric_direction": frozen.spec.metric_direction,
                        "best_git_head": best_git_head,
                    }
                )
            if candidate_features:
                feature_groups.append(candidate_features)

            assessment = research["verifier_assessment"]
            evidence = assessment.get("evidence")
            status = assessment.get("status", "unknown")
            if status != "unknown" or evidence:
                verifier_assessments.append(
                    {
                        **assessment,
                        "candidate_id": record.candidate_id,
                    }
                )

            candidate_pitfalls: list[dict[str, Any]] = []
            for pitfall in research["pitfalls"]:
                item = (
                    dict(pitfall)
                    if isinstance(pitfall, dict)
                    else {"observed_result": str(pitfall)}
                )
                scope = item.get("scope")
                if scope not in {
                    "candidate_local",
                    "feature_family",
                    "evaluation_contract",
                }:
                    scope = "candidate_local"
                confidence = item.get("confidence")
                if confidence not in {"single_observation", "reproduced"}:
                    confidence = "single_observation"
                candidate_pitfalls.append(
                    {
                        **item,
                        "scope": scope,
                        "confidence": confidence,
                        "candidate_id": record.candidate_id,
                    }
                )
            if candidate_pitfalls:
                pitfall_groups.append(candidate_pitfalls)

        def round_robin(
            groups: list[list[dict[str, Any]]],
            limit: int,
        ) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for index in range(max((len(group) for group in groups), default=0)):
                for group in groups:
                    if index < len(group):
                        rows.append(group[index])
                        if len(rows) == limit:
                            return rows
            return rows

        return {
            "feature_ledger": round_robin(feature_groups, 50),
            "verifier_assessments": verifier_assessments[:15],
            "pitfalls": round_robin(pitfall_groups, 30),
            "description": (
                "当前 run 中所有候选的有界研究汇总，包括可见排名 frontier 之外候选的发现。"
            ),
        }

    def build_inherited_research(
        self,
        source_run_id: str,
    ) -> dict[str, Any]:
        source_run = self.source.read_run(source_run_id)
        source_frozen = self.source.read_frozen_spec(source_run.frozen_spec_id)
        records = self.source.read_candidates(source_run_id)
        spec_ids = {candidate_spec_id(source_run, record) for record in records}
        if len(spec_ids) > 1:
            frontier_records = records_by_created(records)[:5]
        else:
            ranking_frozen = (
                self.source.read_frozen_spec(next(iter(spec_ids)))
                if spec_ids else source_frozen
            )
            frontier_records = top_records(records, ranking_frozen.spec, 5)
        visible_candidate_ids = [record.candidate_id for record in frontier_records]
        rollup = self.run_research_rollup(
            records,
            visible_candidate_ids=visible_candidate_ids,
        )

        inherited_features = list(
            source_run.inherited_research.get("feature_ledger", [])
        )
        inherited_pitfalls = list(source_run.inherited_research.get("pitfalls", []))
        current_features = [
            {
                **entry,
                "source_run_id": source_run_id,
                "score_reusable": False,
            }
            for entry in rollup["feature_ledger"]
        ]
        current_pitfalls = [
            {**entry, "source_run_id": source_run_id} for entry in rollup["pitfalls"]
        ]
        features = current_features + inherited_features
        pitfalls = current_pitfalls + inherited_pitfalls
        features = features[:50]
        pitfalls = pitfalls[:30]
        frontier: list[dict[str, Any]] = []
        for record in frontier_records:
            payload = self.history_candidate_payload(record)
            frontier.append(
                {
                    "source_run_id": source_run_id,
                    "candidate_id": record.candidate_id,
                    "score": payload["score"],
                    "frozen_spec_id": payload["frozen_spec_id"],
                    "spec_hash": payload["spec_hash"],
                    "metric_name": payload["metric_name"],
                    "metric_direction": payload["metric_direction"],
                    "score_reusable": False,
                    "best_iteration": payload["best_iteration"],
                    "best_git_head": payload["best_git_head"],
                    "summary": payload.get("summary", ""),
                    "feature_ledger": payload.get("feature_ledger", []),
                }
            )

        return {
            "source_run_id": source_run_id,
            "source_frozen_spec_id": source_run.frozen_spec_id,
            "source_invalidation": {
                "reason": source_run.invalidation_reason,
                "summary": source_run.invalidation_summary,
                "evidence": source_run.invalidation_evidence,
            },
            "frontier": frontier,
            "feature_ledger": features,
            "pitfalls": pitfalls,
            "score_reusable": False,
            "description": (
                "由 policy 控制的继承研究上下文。来源分数仅作历史记录，"
                "每个导入产物或特性都必须重新验证。"
            ),
        }

    def history_candidate_payload(
        self,
        record: CandidateRecord,
        *,
        run_worker_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self.source.read_run(record.task.run_id)
        frozen = self.source.read_frozen_spec(candidate_spec_id(run, record))
        spec = frozen.spec
        score_report = record.score_report
        latest_frozen = (
            self.source.read_frozen_spec(score_report.frozen_spec_id or run.frozen_spec_id)
            if score_report is not None else None
        )
        active_score_report = (
            score_report if latest_frozen is not None
            and latest_frozen.frozen_spec_id == frozen.frozen_spec_id else None
        )
        best_iteration = best_iteration_record(record, spec.metric_direction)
        evidence_score = (
            best_iteration.score
            if best_iteration is not None
            else active_score_report.aggregate_score
            if active_score_report
            else None
        )
        evidence_passed = (
            True
            if best_iteration is not None
            else active_score_report.process_passed
            if active_score_report
            else None
        )
        metrics: dict[str, Any] = {}
        namespaced_metrics: dict[str, Any] = {}
        metrics_omitted = False
        log_paths: list[str] = []
        latest_verifier_summaries: list[dict[str, Any]] = []
        latest_failure_classes: list[str] = []
        latest_log_paths: list[str] = []

        score_report_is_evidence = bool(
            active_score_report
            and active_score_report.process_passed
            and active_score_report.aggregate_score == evidence_score
        )
        if best_iteration is not None:
            log_paths = list(best_iteration.log_paths)
            namespaced_metrics, metrics_omitted = public_metric_projection(best_iteration.metrics)
        if score_report:
            for result in score_report.verifier_results:
                if result.failure_class:
                    latest_failure_classes.append(result.failure_class)
                if result.log_path:
                    latest_log_paths.append(str(result.log_path))
                latest_verifier_summaries.append(
                    {
                        "name": result.name,
                        "role": result.role,
                        "passed": result.passed,
                        "score": result.score,
                        "failure_class": result.failure_class,
                        "log_path": str(result.log_path) if result.log_path else None,
                    }
                )
            if score_report_is_evidence and best_iteration is None:
                namespaced_metrics, metrics_omitted = public_metric_projection({
                    result.name: result.metrics
                    for result in score_report.verifier_results if result.metrics
                })

        for verifier_metrics in namespaced_metrics.values():
            if isinstance(verifier_metrics, dict):
                metrics.update({key: value for key, value in verifier_metrics.items() if key not in metrics})

        key_metrics = {
            key: value
            for key, value in metrics.items()
            if key
            not in {
                "returncode",
                "elapsed_seconds",
            }
            and isinstance(value, int | float | bool | str)
        }

        strategy_metadata = dict(record.task.strategy_metadata)
        if (
            run_worker_policy is not None
            and strategy_metadata.get("worker_policy") == run_worker_policy
        ):
            # Identical static policy already appears once at run level.
            strategy_metadata.pop("worker_policy", None)

        agent_sessions = self.agent_session_payloads_for_candidate(
            record.task.run_id,
            record.candidate_id,
        )
        research_summary = self.candidate_research_summary(
            record.task.run_id,
            record.candidate_id,
        )
        assessment = research_summary["verifier_assessment"]
        research_empty = (
            not research_summary["summary"]
            and not research_summary["key_results"]
            and not research_summary["pitfalls"]
            and not research_summary["blockers"]
            and not research_summary["next_steps"]
            and assessment["status"] == "unknown"
            and not assessment["evidence"]
            and not assessment["impact"]
        )
        risk_notes = [
            (
                "Condition: {condition}; failed approach: {failed_approach}; "
                "reason: {reason}; recommendation: {recommendation}".format(
                    condition=pitfall.get("condition", "the recorded condition"),
                    failed_approach=pitfall.get("failed_approach", "the approach"),
                    reason=pitfall.get("reason", "the recorded reason"),
                    recommendation=pitfall.get("recommendation", "avoid repeating it"),
                )
                if isinstance(pitfall, dict)
                else str(pitfall)
            )
            for pitfall in research_summary["pitfalls"]
        ]

        payload = {
            "candidate_id": record.candidate_id,
            "parent_id": record.task.parent_id,
            "parent_candidate_ids": record.task.parent_candidate_ids,
            "base_candidate_id": record.task.base_candidate_id,
            "plan_id": record.task.plan_id,
            "status": record.status,
            "hypothesis": record.task.hypothesis,
            "intent": record.task.proposal.intent
            if record.task.proposal
            else record.task.hypothesis,
            "strategy_metadata": strategy_metadata,
            "workspace": str(record.task.workspace),
            "agent_sessions": agent_sessions,
            "changed_files": record.detected_changed_files,
            "touched_denied_files": record.touched_denied_files,
            "changed_outside_allowed": record.changed_outside_allowed,
            "process_passed": evidence_passed,
            "score": evidence_score,
            "metric_name": spec.metric_name,
            "metric_direction": spec.metric_direction,
            "evidence_source": "best_iteration"
            if best_iteration
            else "latest_score_report",
            "best_iteration": best_iteration.iteration if best_iteration else None,
            "best_git_head": best_iteration.git_head if best_iteration else None,
            "latest_process_passed": score_report.process_passed
            if score_report
            else None,
            "latest_score": score_report.aggregate_score if score_report else None,
            "latest_disposition": score_report.disposition if score_report else None,
            "workspace_git_head_after_settlement": (
                score_report.workspace_git_head_after_settlement
                if score_report
                else None
            ),
            "latest_failure_classes": latest_failure_classes,
            "latest_verifiers": latest_verifier_summaries,
            "latest_log_paths": latest_log_paths,
            "key_metrics": key_metrics,
            "metrics": namespaced_metrics,
            "metrics_omitted": metrics_omitted,
            "frozen_spec_id": frozen.frozen_spec_id,
            "spec_hash": frozen.spec_hash,
            "active_frozen_spec_id": frozen.frozen_spec_id,
            "active_spec_hash": frozen.spec_hash,
            "spec_revisions": [revision.model_dump(mode="json") for revision in record.spec_revisions],
            "latest_frozen_spec_id": latest_frozen.frozen_spec_id if latest_frozen else None,
            "latest_spec_hash": latest_frozen.spec_hash if latest_frozen else None,
            "llm_validation": strategy_metadata.get("llm_validation", "off"),
        }
        if record.task.proposal:
            expected_tradeoff = record.task.proposal.expected_tradeoff
            if expected_tradeoff:
                payload["expected_tradeoff"] = expected_tradeoff
            search_action = record.task.proposal.metadata.get("search_action")
            if search_action is not None:
                payload["search_action"] = search_action
        if not research_empty:
            payload.update(
                summary=research_summary["summary"],
                key_results=research_summary["key_results"],
                feature_ledger=research_summary["feature_ledger"],
                next_ideas=research_summary["next_steps"],
                risk_notes=risk_notes,
                blockers=research_summary["blockers"],
                verifier_assessment=research_summary["verifier_assessment"],
                research_summary=research_summary,
            )
        # latest_* already carries the latest verifier state verbatim; keep the
        # best-iteration logs only when they are not that same latest report.
        if log_paths and log_paths != latest_log_paths:
            payload["log_paths"] = log_paths
        return payload

    def agent_session_payloads_for_candidate(
        self,
        run_id: str,
        candidate_id: str,
    ) -> list[dict[str, Any]]:
        # Identity and timing only; full session handles stay behind
        # goal_plus_search_recovery_status(detail=true) and observability reads.
        return [
            {
                "agent_session_id": session.agent_session_id,
                "candidate_id": session.candidate_id,
                "session_handle_display": self.display_session_handle(session),
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "verifier_runs": session.counters.get("verifier_runs", 0),
            }
            for session in self.source.read_agent_sessions(run_id)
            if session.candidate_id == candidate_id
        ]
