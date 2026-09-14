"""Actual dispatch attempt identity and result admission."""

from __future__ import annotations


from goal_plus.domain.goal import (
    GoalPlusRecord,
    GoalPlusWorkEventKind,
    GoalPlusWorkItem,
)


from goal_plus.domain.goal_authorization import (
    GOAL_PLUS_ENTRYPOINT_REQUIRED as GOAL_PLUS_ENTRYPOINT_REQUIRED,
    is_terminal_goal,
)


import hashlib
import json
from typing import Any
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.domain.goal_commands import (
    DEFAULT_WORK_LAUNCH_TTL_SECONDS,
    WORK_EVENT_ALLOWED_STATUSES,
    WORK_EVENT_STATUS,
    WORK_EVENT_SUMMARY_MAX_CHARACTERS,
)
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.ports.goal_state import GoalState
from goal_plus.ports.clock import Clock
from goal_plus.ports.session import NativeMainCaller


class GoalDispatch:
    def __init__(
        self, state: GoalState, clock: Clock, authorization: GoalAuthorization
    ) -> None:
        self.state, self.clock, self.authorization = state, clock, authorization

    def record_work_event(
        self,
        goal_plus_id: str,
        work_item_id: str,
        event: GoalPlusWorkEventKind,
        summary: str,
        *,
        host: str | None = None,
        task_name: str | None = None,
        agent_id: str | None = None,
        transcript_path: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        generation: int | None = None,
        launch_ttl_seconds: int = DEFAULT_WORK_LAUNCH_TTL_SECONDS,
        native_main: NativeMainCaller | None = None,
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            summary = summary.strip()
            if not summary:
                raise ValueError("work event summary must not be empty")
            if len(summary) > WORK_EVENT_SUMMARY_MAX_CHARACTERS:
                raise ValueError("work event summary must not exceed 4000 characters")
            if generation is not None and generation < 1:
                raise ValueError("work attempt generation must be positive")

            record = self.state.read_goal(goal_plus_id)
            if native_main is not None:
                execution = event in {"dispatch", "rework", "bind", "message"}
                self.authorization.require_main_goal_session(
                    goal_plus_id, agent_harness=native_main.agent_harness, session_id=native_main.session_id, execution=execution,
                )
                if execution and record.control.version != native_main.control_version:
                    raise PermissionError("MainSessionMismatch: dispatch belongs to an old activation")
            if (
                is_terminal_goal(record)
                or event == "dispatch"
                and record.status != "active"
            ):
                raise RuntimeError("work events require an active Goal Plus record")
            if event == "dispatch" and record.control.state != "active":
                raise RuntimeError(
                    "GoalPlusResumeRequired: stopped tasks cannot dispatch new work"
                )
            if event not in WORK_EVENT_ALLOWED_STATUSES:
                raise ValueError(f"unknown work event: {event}")
            if event == "dispatch" and not host:
                raise ValueError("subagent dispatch requires a host id")

            now = self.clock.timestamp()
            all_items = list(record.work_items)
            try:
                index, item = self._current_work_item(record, work_item_id)
            except FileNotFoundError:
                if event != "dispatch":
                    raise
                index = len(all_items)
                item = GoalPlusWorkItem(
                    work_item_id=work_item_id,
                    objective=summary,
                    goal_revision=record.goal_revision,
                    status="launching",
                    created_at=now,
                    updated_at=now,
                )
                all_items.append(item)

            execution = record.policy.get("execution")
            bound_host = execution.get("host") if isinstance(execution, dict) else None
            bound_host_id = (
                bound_host.get("host_id") if isinstance(bound_host, dict) else None
            )
            if host and bound_host_id and host != bound_host_id:
                raise RuntimeError(
                    f"work event host {host!r} does not match bound Ultra host {bound_host_id!r}"
                )

            supplied_evidence = evidence or []
            supplied_metadata = metadata or {}
            fenced_event = event in {"bind", "message", "result", "failed"}
            if fenced_event:
                if not attempt_id or generation is None:
                    raise ValueError(
                        f"subagent {event} requires attempt_id and generation"
                    )
                if attempt_id != item.attempt_id or generation != item.generation:
                    if event not in {"result", "failed"}:
                        raise RuntimeError(
                            f"work attempt mismatch for {work_item_id}: "
                            f"expected {item.attempt_id}/{item.generation}"
                        )
                    self.state.append_event(
                        goal_plus_id,
                        "execution_work_event",
                        {
                            "goal_revision": record.goal_revision,
                            "work_item_id": work_item_id,
                            "event": "stale_result",
                            "submitted_event": event,
                            "summary": summary,
                            "host": host,
                            "task_name": task_name,
                            "agent_id": agent_id,
                            "attempt_id": attempt_id,
                            "generation": generation,
                            "current_attempt_id": item.attempt_id,
                            "current_generation": item.generation,
                            "evidence": supplied_evidence,
                            "metadata": supplied_metadata,
                        },
                    )
                    return record
                if event == "bind" and not agent_id:
                    raise ValueError("subagent bind requires an agent_id")
                if (
                    event != "bind"
                    and agent_id
                    and item.agent_id
                    and agent_id != item.agent_id
                ):
                    raise RuntimeError(
                        f"work event agent {agent_id!r} does not match bound agent "
                        f"{item.agent_id!r}"
                    )

            result_digest: str | None = None
            if event == "result":
                result_digest = work_result_digest(
                    summary,
                    agent_id=agent_id or item.agent_id,
                    transcript_path=transcript_path or item.transcript_path,
                    evidence=supplied_evidence,
                    metadata=supplied_metadata,
                )
                if item.status in {"result_ready", "accepted"}:
                    if item.result_digest == result_digest:
                        return record
                    raise RuntimeError(
                        f"conflicting result for work attempt {item.attempt_id}/{item.generation}"
                    )

            if item.status not in WORK_EVENT_ALLOWED_STATUSES[event]:
                raise RuntimeError(
                    f"cannot record {event} while work item {work_item_id} is {item.status}"
                )

            update: dict[str, Any] = {
                "updated_at": now,
                "result_summary": summary if event == "result" else item.result_summary,
                "evidence": [*item.evidence, *supplied_evidence],
                "metadata": {**item.metadata, **supplied_metadata},
            }
            if event == "dispatch":
                if attempt_id is not None or generation is not None:
                    raise ValueError(
                        "subagent dispatch creates its own attempt identity"
                    )
                if agent_id is not None:
                    raise ValueError(
                        "subagent dispatch must bind agent_id after host launch"
                    )
                if not 1 <= launch_ttl_seconds <= 3600:
                    raise ValueError("launch_ttl_seconds must be between 1 and 3600")
                if item.status == "launching" and item.launch_deadline:
                    deadline = outer_deadline_epoch(item.launch_deadline)
                    if self.clock.now() < deadline:
                        raise RuntimeError(
                            f"work attempt {item.attempt_id}/{item.generation} is still launching"
                        )
                attempt_id = self.state.new_attempt_id()
                generation = item.generation + 1
                update.update(
                    {
                        "status": "launching",
                        "objective": summary,
                        "attempt_id": attempt_id,
                        "generation": generation,
                        "launch_deadline": self.clock.timestamp_from_epoch(
                            self.clock.now() + launch_ttl_seconds
                        ),
                        "bound_at": None,
                        "agent_id": None,
                        "transcript_path": None,
                        "result_summary": None,
                        "result_digest": None,
                    }
                )
            elif event == "rework":
                attempt_id = self.state.new_attempt_id()
                generation = item.generation + 1
                update.update(
                    {
                        "status": "active",
                        "objective": summary,
                        "attempt_id": attempt_id,
                        "generation": generation,
                        "launch_deadline": None,
                        "result_summary": None,
                        "result_digest": None,
                    }
                )
            else:
                status = WORK_EVENT_STATUS.get(event)
                if status is not None:
                    update["status"] = status
            if event == "bind":
                update.update({"agent_id": agent_id, "bound_at": now})
            if result_digest is not None:
                update["result_digest"] = result_digest
            for key, value in {
                "host": host,
                "task_name": task_name,
                "transcript_path": transcript_path,
            }.items():
                if value is not None:
                    update[key] = value

            all_items[index] = item.model_copy(update=update)
            updated = record.model_copy(
                update={
                    "work_items": all_items,
                    "updated_at": now,
                }
            )
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id,
                "execution_work_event",
                {
                    "goal_revision": record.goal_revision,
                    "work_item_id": work_item_id,
                    "event": event,
                    "summary": summary,
                    "host": host,
                    "task_name": task_name,
                    "agent_id": agent_id,
                    "attempt_id": attempt_id,
                    "generation": generation,
                    "evidence": supplied_evidence,
                    "metadata": supplied_metadata,
                },
            )
            return updated

    def _current_work_item(
        self,
        record: GoalPlusRecord,
        work_item_id: str,
    ) -> tuple[int, GoalPlusWorkItem]:
        for index, item in enumerate(record.work_items):
            if (
                item.goal_revision == record.goal_revision
                and item.work_item_id == work_item_id
            ):
                return index, item
        raise FileNotFoundError(
            f"work item not found for goal revision {record.goal_revision}: {work_item_id}"
        )


def work_result_digest(
    summary: str,
    *,
    agent_id: str | None,
    transcript_path: str | None,
    evidence: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> str:
    payload = json.dumps(
        {
            "summary": summary,
            "agent_id": agent_id,
            "transcript_path": transcript_path,
            "evidence": evidence,
            "metadata": metadata,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
