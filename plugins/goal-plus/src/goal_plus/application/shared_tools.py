"""Shared-tool publication admission and settlement advisories."""

from typing import Any, Literal
from goal_plus.domain.evidence import ScoreReport
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.shared_tools import (
    SharedToolPublishStatus,
    SharedToolRecord,
    ToolizationAdvisory,
    ToolizationDecision,
)
from goal_plus.ports.state import CandidateRecord

from goal_plus.application.annotation import AnnotationWorkflow
from goal_plus.ports.candidate_settlement import CandidateSettlementState
from goal_plus.ports.clock import Clock
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.candidate_execution import require_worker_iteration_state
from goal_plus.domain.shared_tools import TOOL_INBOX_RELATIVE_PATH, ToolCopyReceipt
from goal_plus.ports.shared_tools import ToolDraft
from goal_plus.ports.shared_tools import (
    SharedToolIterationSettlement,
    ToolPublicationWorkspace,
)


class SharedTools:
    def __init__(
        self,
        source: CandidateSettlementState,
        publication: ToolPublicationWorkspace,
        annotation: AnnotationWorkflow,
        sessions: CandidateSessions,
        clock: Clock,
    ) -> None:
        self.source, self.publication, self.annotation = source, publication, annotation
        self.sessions, self.clock = sessions, clock

    def settle_shared_tool_iteration(
        self,
        *,
        run_id: str,
        candidate_id: str,
        frozen: FrozenSpec,
        record: CandidateRecord,
        report: ScoreReport,
        source_commit: str | None,
        iteration_number: int,
        agent_session_id: str | None,
        toolization_decision: ToolizationDecision | None,
    ) -> SharedToolIterationSettlement:
        limits = frozen.spec.shared_dir
        if not limits.enabled:
            return SharedToolIterationSettlement(
                tools=[],
                errors=[],
                staged_entries=[],
                staged_file_count=0,
                staged_bytes=0,
                consumed_entries=[],
                deduplicated_entries=[],
                publish_status="not_staged",
                toolization_advisories=[],
            )

        shared_inventory = None
        if record.task.share_out_dir is not None:
            try:
                shared_inventory = self.publication.inspect_staging(
                    run_id, record.task.share_out_dir, limits.max_tools_per_iteration
                )
            except Exception as exc:
                shared_inventory = {
                    "entries": [],
                    "errors": [f"staging inspection failed: {exc}"],
                }

        shared_settlement = None
        shared_settlement_error = None
        if (
            report.process_passed
            and agent_session_id is not None
            and record.task.share_out_dir is not None
        ):
            try:
                shared_settlement = self.publication.settle_iteration(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    iteration=iteration_number,
                    source_commit=source_commit,
                    share_out_dir=record.task.share_out_dir,
                    limits=limits,
                    adopted_tool_ids={
                        item.tool_id
                        for candidate in self.source.read_candidates(run_id)
                        for prior_iteration in candidate.iterations
                        for item in prior_iteration.adopted_tools
                    },
                )
            except Exception as exc:
                shared_settlement_error = f"shared tool snapshot failed: {exc}"

        inventory_observed = bool(
            (shared_inventory or {}).get("entries")
            or (shared_inventory or {}).get("errors")
        )
        staged_entries = (
            shared_settlement.staged_entries
            if shared_settlement is not None
            else list((shared_inventory or {}).get("entries", []))
        )
        advisories: list[ToolizationAdvisory] = []
        if agent_session_id is not None:
            if toolization_decision is None:
                advisories.append("toolization_review_missing")
            elif toolization_decision.outcome == "staged" and not staged_entries:
                advisories.append("toolization_stage_missing")
            elif toolization_decision.outcome == "not_applicable" and staged_entries:
                advisories.append("toolization_decision_mismatch")

        if shared_settlement is not None:
            errors = shared_settlement.errors
        elif shared_settlement_error is not None:
            errors = [shared_settlement_error]
        else:
            errors = list((shared_inventory or {}).get("errors", []))

        publish_status: SharedToolPublishStatus = "not_staged"
        if shared_settlement is not None:
            if shared_settlement.tools:
                publish_status = (
                    "partially_published" if shared_settlement.errors else "published"
                )
            elif shared_settlement.consumed_entries:
                publish_status = "consumed_unchanged"
            elif shared_settlement.errors:
                publish_status = "snapshot_rejected"
        elif shared_settlement_error is not None:
            publish_status = "snapshot_error"
        elif inventory_observed and agent_session_id is None:
            publish_status = "skipped_unattributed_verifier"
        elif inventory_observed and not report.process_passed:
            publish_status = "skipped_failed_verifier"
        return SharedToolIterationSettlement(
            tools=(shared_settlement.tools if shared_settlement is not None else []),
            errors=errors,
            staged_entries=staged_entries,
            staged_file_count=(
                shared_settlement.staged_file_count
                if shared_settlement is not None
                else 0
            ),
            staged_bytes=(
                shared_settlement.staged_bytes if shared_settlement is not None else 0
            ),
            consumed_entries=(
                shared_settlement.consumed_entries
                if shared_settlement is not None
                else []
            ),
            deduplicated_entries=(
                shared_settlement.deduplicated_entries
                if shared_settlement is not None
                else []
            ),
            publish_status=publish_status,
            toolization_advisories=advisories,
        )

    def prepare_worker_toolization_decision(
        self,
        *,
        run_id: str,
        agent_session_id: str,
        toolization_decision: ToolizationDecision,
    ) -> ToolizationDecision | None:
        with self.source.lock_run(run_id):
            run = self.source.read_run(run_id)
            frozen = self.source.read_frozen_spec(run.frozen_spec_id)
            return self.normalize_toolization_decision(
                run_id=run_id, frozen=frozen, scope="process",
                agent_session_id=agent_session_id,
                toolization_decision=toolization_decision,
            )

    def normalize_toolization_decision(
        self,
        *,
        run_id: str,
        frozen: FrozenSpec,
        scope: Literal["process", "promotion"],
        agent_session_id: str | None,
        toolization_decision: ToolizationDecision | dict[str, Any] | None,
    ) -> ToolizationDecision | None:
        if not frozen.spec.shared_dir.enabled:
            toolization_outcome = (
                toolization_decision.get("outcome")
                if isinstance(toolization_decision, dict)
                else getattr(toolization_decision, "outcome", None)
            )
            if toolization_outcome == "not_applicable":
                toolization_decision = None
        normalized = (
            ToolizationDecision.model_validate(toolization_decision)
            if toolization_decision is not None
            else None
        )
        if normalized is not None and (scope != "process" or agent_session_id is None):
            raise ValueError(
                "toolization_decision is only valid for worker process verifier calls"
            )
        if normalized is not None and not frozen.spec.shared_dir.enabled:
            raise ValueError("toolization_decision requires shared_dir.enabled=true")
        if (
            normalized is not None
            and normalized.outcome == "not_applicable"
            and normalized.exclusion
            in {
                "existing_family_sufficient",
                "no_material_tool_delta",
                "max_published_versions_reached",
            }
            and not self.publication.tool_family_catalog(
                run_id,
                max_published_versions_per_family=(
                    frozen.spec.shared_dir.max_published_versions_per_family
                ),
            )
        ):
            raise ValueError(
                "toolization exclusion "
                f"{normalized.exclusion!r} requires an existing shared tool family; "
                "use 'no_toolizable_material' when no reusable tool material exists"
            )
        return normalized

    def resolve_shared_tool(
        self,
        run_id: str,
        tool_id: str,
        snapshot_hash: str,
        *,
        require_discoverable: bool = True,
    ) -> SharedToolRecord:
        available = {
            tool.tool_id: tool
            for record in self.source.read_candidates(run_id)
            for iteration in record.iterations
            for tool in iteration.shared_tools
        }
        tool = available.get(tool_id)
        if tool is None:
            raise ValueError(f"tool_id is not published in this run: {tool_id}")
        if tool.snapshot_hash != snapshot_hash:
            raise ValueError(f"tool snapshot_hash mismatch for {tool_id}")
        if not self.tool_view_is_published(run_id, tool):
            raise ValueError(
                f"tool is not discoverable before its Tool View is published: {tool_id}"
            )
        if require_discoverable:
            self.annotation.reconcile_tool_heads(run_id)
            discoverable = self.publication.discoverable_tool_ids(run_id)
            if tool.tool_id not in discoverable:
                raise ValueError(
                    f"tool is not the current discoverable family head: {tool_id}"
                )
        return tool

    def tool_view_is_published(self, run_id: str, tool: SharedToolRecord) -> bool:
        task = self.annotation.task(run_id, tool.candidate_id, tool.iteration)
        if task is None or task.state != "completed" or task.view is None:
            return False
        return any(
            view.tool_id == tool.tool_id
            and view.snapshot_hash == tool.snapshot_hash
            and view.source_commit == (tool.source_commit or task.attempt_commit)
            for view in task.view.tool_views
        )

    def stage(
        self,
        agent_session_id: str,
        name: str,
        summary: str,
        entrypoint: str,
        candidate_relative_source_paths: list[str],
        publication_intent: str = "new",
        supersedes_tool_id: str | None = None,
        capability_ids: list[str] | None = None,
        coverage_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        session = self.sessions.session(agent_session_id)
        with self.source.lock_verifier(session.run_id, session.candidate_id):
            with self.source.lock_run(session.run_id):
                self.sessions.session(session.agent_session_id, session.run_id)
                run = self.source.read_run(session.run_id)
                self.sessions.authorization.require_search_execution(run.run_id)
                require_worker_iteration_state(run, "stage shared tool")
                frozen = self.source.read_frozen_spec(run.frozen_spec_id)
                if not frozen.spec.shared_dir.enabled:
                    raise ValueError("tool staging requires shared_dir.enabled=true")
                record = self.source.read_candidate(
                    session.run_id, session.candidate_id
                )
                if record.status not in {"created", "evaluated"}:
                    raise RuntimeError(
                        f"cannot stage a tool for candidate in status {record.status}"
                    )
                if record.task.share_out_dir is None:
                    raise RuntimeError(
                        "shared-dir candidate has no share-out directory"
                    )
                limits = frozen.spec.shared_dir
                return self.publication.stage(
                    run.run_id,
                    record.task.workspace,
                    record.task.share_out_dir,
                    limits,
                    ToolDraft(
                        name=name,
                        summary=summary,
                        entrypoint=entrypoint,
                        candidate_relative_source_paths=candidate_relative_source_paths,
                        publication_intent=publication_intent,
                        supersedes_tool_id=supersedes_tool_id,
                        capability_ids=capability_ids,
                        coverage_keys=coverage_keys,
                    ),
                )

    def copy(
        self, agent_session_id: str, tool_id: str, snapshot_hash: str
    ) -> dict[str, Any]:
        session = self.sessions.session(agent_session_id)
        with self.source.lock_verifier(session.run_id, session.candidate_id):
            with self.source.lock_run(session.run_id):
                self.sessions.session(session.agent_session_id, session.run_id)
                run = self.source.read_run(session.run_id)
                self.sessions.authorization.require_search_execution(run.run_id)
                require_worker_iteration_state(run, "copy shared tool")
                frozen = self.source.read_frozen_spec(run.frozen_spec_id)
                if not frozen.spec.shared_dir.enabled:
                    raise ValueError("tool copy requires shared_dir.enabled=true")
                record = self.source.read_candidate(
                    session.run_id, session.candidate_id
                )
                if (
                    len(record.pending_tool_copies)
                    >= frozen.spec.shared_dir.max_tools_per_iteration
                ):
                    raise ValueError(
                        "pending tool copies exceed shared_dir max_tools_per_iteration"
                    )
                if any(item.tool_id == tool_id for item in record.pending_tool_copies):
                    raise ValueError(
                        "tool already copied for the next verifier iteration: "
                        f"{tool_id}"
                    )
                tool = self.resolve_shared_tool(session.run_id, tool_id, snapshot_hash)
                candidate_base_artifact_ref = record.results_ledger_artifact_ref
                if (
                    candidate_base_artifact_ref is None
                    or candidate_base_artifact_ref.kind != "git_commit"
                ):
                    raise RuntimeError("tool copy requires a Git-backed candidate")
                receipt_id = self.publication.new_receipt_id()
                inbox_path = (
                    record.task.workspace / TOOL_INBOX_RELATIVE_PATH / receipt_id
                )
                try:
                    self.publication.materialize_tool(session.run_id, tool, inbox_path)
                except Exception:
                    self.publication.remove_inbox(inbox_path)
                    raise
                receipt = ToolCopyReceipt(
                    receipt_id=receipt_id,
                    tool_id=tool.tool_id,
                    snapshot_hash=tool.snapshot_hash,
                    source_commit=tool.source_commit,
                    agent_session_id=agent_session_id,
                    candidate_base_git_head=candidate_base_artifact_ref.id,
                    inbox_path=inbox_path,
                    copied_at=self.clock.timestamp(),
                )
                record.pending_tool_copies.append(receipt)
                self.source.write_candidate(session.run_id, record)
        return receipt.model_dump(mode="json")

    def catalog(self, run_id: str) -> list[dict[str, Any]]:
        with self.source.lock_run(run_id):
            run = self.source.read_run(run_id)
            frozen = self.source.read_frozen_spec(run.frozen_spec_id)
            if not frozen.spec.shared_dir.enabled:
                return []
            self.annotation.reconcile_tool_heads(run_id)
            return self.publication.tool_family_catalog(
                run_id,
                max_published_versions_per_family=(
                    frozen.spec.shared_dir.max_published_versions_per_family
                ),
            )
