"""Goal triage, spec draft, Search linking and promoted-result settlement."""

from __future__ import annotations
from typing import Any
from goal_plus.domain.goal_authorization import validate_search_command_config


from goal_plus.domain.goal import (
    GoalPlusLinkedSearch,
    GoalPlusNextAction,
    GoalPlusRecord,
    GoalPlusSpecDraft,
    GoalPlusSpecDraftInput,
    GoalPlusTriage,
)

from goal_plus.domain.goal_authorization import (
    is_terminal_goal,
)

from goal_plus.domain.search import (
    SearchSpec,
    SearchSpecDraft,
)

from goal_plus.ports.goal_state import GoalState
from goal_plus.ports.clock import Clock
from goal_plus.ports.goal_search import GoalSearchArtifacts
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.domain.goal_closeout import (
    triage_phase_and_action,
    spec_draft_next_action,
)


class GoalSearch:
    def __init__(
        self,
        state: GoalState,
        clock: Clock,
        artifacts: GoalSearchArtifacts,
        authorization: GoalAuthorization,
    ) -> None:
        self.state, self.clock = state, clock
        self.artifacts = artifacts
        self.authorization = authorization

    def record_triage(
        self,
        goal_plus_id: str,
        triage: GoalPlusTriage | dict[str, Any],
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            current = self.state.read_goal(goal_plus_id)
            if current.control.state != "active":
                raise RuntimeError(
                    "GoalPlusResumeRequired: task control does not permit new work"
                )
            parsed = (
                triage
                if isinstance(triage, GoalPlusTriage)
                else GoalPlusTriage.model_validate(triage)
            )
            record = self.state.read_goal(goal_plus_id)
            phase, next_action = triage_phase_and_action(parsed)
            updated = record.model_copy(
                update={
                    "triage": parsed,
                    "phase": phase,
                    "next_action": next_action,
                    "updated_at": self.clock.timestamp(),
                }
            )
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id, "triage_recorded", parsed.model_dump(mode="json")
            )
            return updated

    def save_spec_draft(
        self,
        goal_plus_id: str,
        spec_draft: GoalPlusSpecDraft | dict[str, Any],
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            current = self.state.read_goal(goal_plus_id)
            if current.control.state != "active":
                raise RuntimeError(
                    "GoalPlusResumeRequired: task control does not permit new work"
                )
            parsed = (
                spec_draft
                if isinstance(spec_draft, GoalPlusSpecDraft)
                else GoalPlusSpecDraftInput.model_validate(spec_draft)
            )
            if parsed.confidence == "high" and not parsed.open_questions:
                search_spec = parsed.search_spec
                search_spec_data = (
                    search_spec.model_dump(exclude_none=True)
                    if isinstance(search_spec, SearchSpecDraft)
                    else search_spec
                )
                resolved_search_spec = SearchSpec.model_validate(search_spec_data)
            else:
                resolved_search_spec = None
            record = self.state.read_goal(goal_plus_id)
            if resolved_search_spec is not None:
                validate_search_command_config(
                    record.command_config,
                    resolved_search_spec,
                )
            origin = parsed.origin or (
                record.triage.identified_at
                if record.triage is not None
                else "in_progress"
            )
            parsed = parsed.model_copy(update={"origin": origin})
            next_action = spec_draft_next_action(parsed)
            updated = record.model_copy(
                update={
                    "phase": "spec_discovery",
                    "spec_draft": parsed,
                    "next_action": next_action,
                    "updated_at": self.clock.timestamp(),
                }
            )
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id, "spec_draft_saved", parsed.model_dump(mode="json")
            )
            return updated

    def link_search_run(
        self,
        goal_plus_id: str,
        frozen_spec_id: str,
        run_id: str,
        *,
        expected_revision: int | None = None,
        expected_control_version: int | None = None,
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            current = self.state.read_goal(goal_plus_id)
            if (current.status != "active"
                or expected_revision is not None and current.goal_revision != expected_revision
                or expected_control_version is not None and current.control.version != expected_control_version):
                raise RuntimeError("SearchCreateStale: Goal changed before Search association")
            if current.control.state != "active":
                raise RuntimeError(
                    "GoalPlusResumeRequired: task control does not permit new work"
                )
            if not run_id.startswith("run_") or any(
                not (character.isalnum() or character in {"_", "-"})
                for character in run_id
            ):
                raise ValueError(f"invalid search run id: {run_id}")
            search_run = self.artifacts.read_search(run_id)
            if search_run is None:
                raise FileNotFoundError(
                    f"search run not found: {run_id}. Call goal_plus_search_create and use its returned run_id."
                )
            actual_frozen_spec_id = search_run.frozen_spec_id
            if actual_frozen_spec_id != frozen_spec_id:
                raise ValueError(
                    f"search run {run_id} belongs to frozen spec {actual_frozen_spec_id}; "
                    f"cannot link it as {frozen_spec_id}"
                )
            owner = self.authorization.run_owner(run_id)
            if owner is not None and owner != goal_plus_id:
                raise RuntimeError(
                    f"search run {run_id} is already linked to Goal Plus {owner}"
                )
            record = self.state.read_goal(goal_plus_id)
            existing = next(
                (task for task in record.search_tasks if task.run_id == run_id),
                None,
            )
            if existing is not None:
                if existing.frozen_spec_id not in {None, frozen_spec_id}:
                    raise ValueError(
                        f"search run {run_id} is already linked as frozen spec "
                        f"{existing.frozen_spec_id}; cannot relink it as {frozen_spec_id}"
                    )
                return record

            now = self.clock.timestamp()
            linked = GoalPlusLinkedSearch(
                goal_revision=record.goal_revision,
                frozen_spec_id=frozen_spec_id,
                run_id=run_id,
                linked_at=now,
            )
            search_tasks = [*record.search_tasks, linked]
            updated = record.model_copy(
                update={
                    "phase": "search",
                    "search_tasks": search_tasks,
                    "linked_search": linked,
                    "next_action": GoalPlusNextAction(
                        kind="drive_search_run",
                        description="推进已链接的 Search MCP run，完成候选验证、选择、报告和提升。",
                        required=True,
                    ),
                    "updated_at": now,
                }
            )
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id,
                "search_linked",
                {
                    "goal_revision": record.goal_revision,
                    "frozen_spec_id": frozen_spec_id,
                    "run_id": run_id,
                },
            )
            return updated

    def record_search_result(
        self,
        goal_plus_id: str,
        run_id: str,
        selected_candidate_id: str | None = None,
        report_path: str | None = None,
        promotion_artifact_path: str | None = None,
        summary: str | None = None,
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            record = self.state.read_goal(goal_plus_id)
            if is_terminal_goal(record):
                raise RuntimeError(
                    "Cannot settle Search into a terminal or cleared Goal Plus task"
                )
            task_index = next(
                (
                    index
                    for index, task in enumerate(record.search_tasks)
                    if task.run_id == run_id
                ),
                None,
            )
            if task_index is None:
                raise RuntimeError(
                    f"Goal Plus {goal_plus_id} is not linked to search run {run_id}."
                )
            run_state = self.artifacts.read_search(run_id)
            if run_state is None:
                raise RuntimeError(f"Search run state does not exist: {run_id}")
            if run_state.state != "promoted":
                raise RuntimeError(
                    "Call goal_plus_search_promote for the selected candidate before recording "
                    "the Goal Plus search result."
                )
            if (
                run_state.promotion_mode == "apply"
                and run_state.promotion_state != "applied"
            ):
                raise RuntimeError(
                    "Call goal_plus_search_apply_promotion before recording a Search result whose "
                    "workspace.promotion_mode is 'apply'."
                )
            runtime_selected_candidate_id = run_state.selected_candidate_id
            if not runtime_selected_candidate_id:
                raise RuntimeError("Promoted search run has no selected candidate.")
            if (
                selected_candidate_id is not None
                and selected_candidate_id != runtime_selected_candidate_id
            ):
                raise RuntimeError(
                    "Goal Plus selected_candidate_id does not match the promoted search run."
                )
            selected_candidate_id = str(runtime_selected_candidate_id)
            report_path = self.artifacts.report_path(run_id, report_path)
            html_report_path = self.artifacts.html_report_path(run_id)
            promotion_artifact_path = self.artifacts.promotion_artifact_path(
                run_id,
                selected_candidate_id,
                promotion_artifact_path,
            )
            if promotion_artifact_path is None or not self.artifacts.artifact_available(
                promotion_artifact_path
            ):
                raise RuntimeError("Search promotion artifact does not exist.")
            now = self.clock.timestamp()
            linked = record.search_tasks[task_index].model_copy(
                update={
                    "run_id": run_id,
                    "selected_candidate_id": selected_candidate_id,
                    "report_path": report_path,
                    "html_report_path": html_report_path,
                    "promotion_artifact_path": promotion_artifact_path,
                    "summary": summary,
                    "result_recorded_at": now,
                }
            )
            search_tasks = list(record.search_tasks)
            search_tasks[task_index] = linked
            is_current_task = (
                linked.goal_revision == record.goal_revision
                and record.linked_search is not None
                and record.linked_search.run_id == run_id
            )
            update: dict[str, Any] = {
                "search_tasks": search_tasks,
                "updated_at": now,
            }
            if is_current_task:
                update.update(
                    {
                        "phase": "final_audit",
                        "linked_search": linked,
                        "next_action": GoalPlusNextAction(
                            kind="audit_raw_goal",
                            description="将原始目标与当前证据逐项核对，然后才能把 goal-plus 标记为完成。",
                            required=True,
                        ),
                    }
                )
            updated = record.model_copy(update=update)
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id,
                "search_result_recorded",
                linked.model_dump(mode="json"),
            )
            return updated
