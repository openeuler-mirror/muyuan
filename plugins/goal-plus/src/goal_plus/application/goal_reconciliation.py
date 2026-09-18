"""Durable current-revision recovery agenda and closeout gate."""

from __future__ import annotations
from typing import Any
import json


from goal_plus.domain.goal import (
    GoalPlusRecord,
)

from goal_plus.domain.goal_authorization import (
    is_terminal_goal,
)


from goal_plus.ports.goal_state import GoalReadState
from goal_plus.ports.clock import Clock
from goal_plus.application.recovery_queries import CandidateRecoveryQueries


class GoalReconciliation:
    def __init__(
        self, state: GoalReadState, clock: Clock, search: CandidateRecoveryQueries
    ) -> None:
        self.state, self.clock = state, clock
        self.search = search

    def reconciliation(self, goal_plus_id: str) -> dict[str, Any]:
        """Durable Main agenda, without performing host process inspection.

        A restored checkpoint is not execution. Keep the remaining launch and
        evidence steps visible on every read, regardless of Main's current phase.
        """

        record = self.state.read_goal(goal_plus_id)
        runs = []
        for task in record.search_tasks:
            if task.goal_revision != record.goal_revision:
                continue
            try:
                state = self.search.status(task.run_id)
            except (FileNotFoundError, RuntimeError, ValueError) as exc:
                runs.append(
                    {
                        "run_id": task.run_id,
                        "next_step": "inspect_unavailable_state",
                        "error": str(exc),
                    }
                )
                continue
            if state["state"] in {"promoted", "aborted", "failed"}:
                continue
            candidates = []
            for candidate in state["candidates"]:
                recovery = candidate["recovery"]
                step = "continue_candidate"
                if recovery:
                    current = [
                        s
                        for s in candidate["sessions"]
                        if s["execution_generation"] == candidate["generation"]
                    ]
                    if recovery["state"] != "restored":
                        step = "stop_old_execution_then_restore"
                    elif not current:
                        step = "start_fresh_worker"
                    elif not any(s.get("bound") for s in current):
                        step = "reconcile_launch_then_bind"
                    elif candidate["fresh_recovery_evidence"]:
                        step = "continue_search"
                    else:
                        step = "await_fresh_evidence"
                elif verification := candidate.get("verification"):
                    step = "inspect_verifier_execution" if verification["state"] == "running" else "retry_verifier_attempt"
                candidates.append({**candidate, "next_step": step})
            runs.append(
                {
                    "run_id": task.run_id,
                    "state": state["state"],
                    "candidates": candidates,
                }
            )
        return {
            "control": record.control.model_dump(mode="json"),
            "resume_eligible": not is_terminal_goal(record)
            and record.active_session is not None
            and record.active_session.state not in {"detached", "stale"},
            "outer_deadline_at": record.outer_deadline_at,
            "runs": runs,
        }

    def reconciliation_context(self, goal_plus_id: str, *, compact: bool = False) -> str:
        """Agenda text for stop continuations; compact keeps only live state.

        The full instructions ride along in the first stop injection; compact
        stop prompts (host-tracked, compaction-aware) keep the one-line form
        because the JSON agenda is the only part that changes between stops.
        """
        runs = self.reconciliation(goal_plus_id)["runs"]
        if not runs:
            return ""
        agenda = []
        for run in runs:
            item: dict[str, Any] = {"run_id": run["run_id"]}
            if "next_step" in run:
                item["next_step"] = run["next_step"]
            else:
                item["candidates"] = [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "generation": candidate["generation"],
                        "next_step": candidate["next_step"],
                        **(
                            {"recovery_id": candidate["recovery"]["recovery_id"]}
                            if candidate["recovery"]
                            else {}
                        ),
                    }
                    for candidate in run["candidates"]
                ]
            agenda.append(item)
        if compact:
            return (
                "Search reconciliation:\n"
                + json.dumps(agenda, ensure_ascii=False)
                + "\n按 next_step 推进（continue_candidate → goal_plus_session_run 新 "
                "call_id；仅显式 recovery_id 事务用 recovery 工具）；先处理最新用户消息，"
                "每轮重读 goal_plus_status。\n"
            )
        return (
            "Search reconciliation:\n"
            + json.dumps(agenda, ensure_ascii=False)
            + "\n按 next_step 推进；continue_candidate 使用 goal_plus_session_run 和新的 call_id。"
            "该入口自行启动或续接同一 candidate，并保留原 workspace。"
            "仅已有 recovery_id 的显式 checkpoint 事务使用 Search recovery 工具。"
            "先处理最新用户消息，每轮重读 goal_plus_status；非 Search 工作后仍继续此清单。"
        )

    def require_idle_execution(
        self,
        record: GoalPlusRecord,
        action: str,
    ) -> None:
        active_items = [
            item
            for item in record.work_items
            if item.goal_revision == record.goal_revision
            and item.status in {"launching", "active"}
        ]
        if active_items:
            detail = ", ".join(
                f"{item.work_item_id}={item.status}" for item in active_items
            )
            raise RuntimeError(f"cannot {action}; active subagent dispatches: {detail}")
        for run in self.reconciliation(record.goal_plus_id)["runs"]:
            for candidate in run.get("candidates", []):
                if (
                    candidate["recovery"]
                    and candidate["next_step"] != "continue_search"
                ):
                    raise RuntimeError(
                        f"cannot {action}; SearchRecoveryIncomplete: {run['run_id']}/{candidate['candidate_id']} "
                        f"still requires {candidate['next_step']}"
                    )
