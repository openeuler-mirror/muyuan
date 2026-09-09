"""Read-only candidate admission, session binding and settled-result projections."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from goal_plus.application.candidate_execution import (
    require_current_session,
    require_run_not_invalidated,
    require_worker_iteration_state,
)
from goal_plus.application.search_context import SearchContext
from goal_plus.domain.search import AgentSessionRecord, CandidateRecovery
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.domain.worker_policy import (
    candidate_worker_budget,
    normalize_worker_budget_override,
)
from goal_plus.ports.candidate_host import CandidateFileWriteBoundary, CandidateHostSource


@dataclass(frozen=True)
class CandidateLeaseContext:
    outer_deadline: float | None
    reserve_seconds: float
    invalidated: bool
    recovering_candidates: frozenset[str]


class CandidateHostQueries:
    def __init__(self, source: CandidateHostSource, context: SearchContext) -> None:
        self.source = source
        self.context = context

    def worker_host(self, run_id: str) -> str:
        run = self.source.read_run(run_id)
        return self.source.read_frozen_spec(
            run.frozen_spec_id
        ).worker_host

    def uses_adaptive_search(self, run_id: str) -> bool:
        try:
            run = self.source.read_run(run_id)
            strategy = self.source.read_frozen_spec(run.frozen_spec_id).spec.strategy
        except (OSError, ValueError):
            return False
        return (
            strategy.orchestration_mode == "adaptive_search"
            and strategy.search_scheduler is not None
        )

    def requires_immediate_stop(self, session: AgentSessionRecord) -> bool:
        try:
            record = self.source.read_candidate(session.run_id, session.candidate_id)
        except (OSError, ValueError):
            return False
        for iteration in reversed(record.iterations):
            if iteration.agent_session_id not in {None, session.agent_session_id}:
                continue
            return (
                iteration.failure_class == "VerifierWorkspaceSideEffect"
                or iteration.metrics.get("infrastructure_failure") is True
                or iteration.metrics.get("candidate_action") == "stop_and_report"
            )
        return False

    def retired_by_allocation(self, run_id: str, candidate_id: str) -> bool:
        try:
            record = self.source.read_candidate(run_id, candidate_id)
        except (OSError, ValueError):
            return False
        return record.allocation_eligibility == "retired" and bool(
            record.retired_by_decision_id
        )

    def pi_pool_limit(self, run_id: str, max_parallel: int | None) -> int:
        run = self.source.read_run(run_id)
        require_run_not_invalidated(run, "open or submit Pi pool work")
        frozen = self.source.read_frozen_spec(run.frozen_spec_id)
        if frozen.worker_host != "pi-rpc":
            raise ValueError(
                "Pi worker pools require a Pi Main with a local workspace backend; "
                f"got {frozen.worker_host!r}"
            )
        frozen_limit = int(frozen.spec.budget.max_parallel)
        selected = frozen_limit if max_parallel is None else int(max_parallel)
        if selected <= 0:
            raise ValueError("max_parallel must be > 0")
        if selected > frozen_limit:
            raise ValueError(
                f"max_parallel {selected} exceeds frozen Search limit {frozen_limit}"
            )
        return selected

    def validate_candidate(self, run_id: str, candidate_id: str) -> None:
        record = self.source.read_candidate(run_id, candidate_id)
        if record.status not in {"created", "evaluated"}:
            raise RuntimeError(
                f"cannot dispatch candidate {candidate_id} in status {record.status}"
            )

    def worker_budget(
        self,
        run_id: str,
        candidate_id: str,
        worker_host: str,
        override: dict[str, Any] | None,
    ) -> dict[str, Any]:
        run = self.source.read_run(run_id)
        frozen = self.source.read_frozen_spec(run.frozen_spec_id)
        record = self.source.read_candidate(run_id, candidate_id)
        budget = normalize_worker_budget_override(
            worker_host=worker_host,
            worker_budget={
                **(candidate_worker_budget(frozen, record.task) or {}),
                **(override or {}),
            },
        )
        assert budget is not None
        return budget

    def lease_context(self, run_id: str) -> CandidateLeaseContext:
        run = self.source.read_run(run_id)
        frozen = self.source.read_frozen_spec(run.frozen_spec_id)
        config = frozen.spec.strategy.config
        return CandidateLeaseContext(
            outer_deadline=outer_deadline_epoch(self.context.outer_deadline(run_id)),
            reserve_seconds=float(
                config.get("closeout_reserve_seconds")
                or config.get("reserve_closeout_seconds")
                or 0
            ),
            invalidated=bool(run.invalidated_at),
            recovering_candidates=frozenset(
                candidate.candidate_id
                for candidate in self.source.read_candidates(run_id)
                if candidate.recovery is not None
                and candidate.recovery.state != "restored"
            ),
        )

    def sessions(
        self, run_id: str, candidate_id: str | None = None, host: str | None = None
    ) -> list[AgentSessionRecord]:
        return [
            item
            for item in self.source.read_agent_sessions(run_id)
            if (candidate_id is None or item.candidate_id == candidate_id)
            and (host is None or item.host == host)
        ]

    def session(
        self, agent_session_id: str, run_id: str | None = None
    ) -> AgentSessionRecord:
        session = self.source.read_agent_session(agent_session_id, run_id)
        return require_current_session(
            session, self.source.read_candidate(session.run_id, session.candidate_id)
        )

    def launch_task_session(self, task_name: str, *, host: str) -> AgentSessionRecord | None:
        """Resolve a recorded launch identity; callers must still check admission."""
        matches = [
            session for identity in self.source.read_agent_session_ids()
            if (session := self.source.read_agent_session(identity)).host == host
            and session.launch.get("task_name") == task_name
        ]
        if len(matches) > 1:
            raise RuntimeError("native task name does not identify a unique candidate session")
        return matches[0] if matches else None

    def find_session(self, agent_session_id: str) -> AgentSessionRecord | None:
        """Historical host observation only; this does not grant execution admission."""
        try:
            return self.source.read_agent_session(agent_session_id)
        except (OSError, ValueError, RuntimeError):
            return None

    def require_native_execution(
        self, agent_session_id: str, *, now: float,
        spawn_input: Mapping[str, object] | None = None,
    ) -> AgentSessionRecord:
        from goal_plus.domain.adaptive.scheduler import confirmation_execution_barrier

        session = self.session(agent_session_id)
        if spawn_input is not None:
            if session.host != "codex" or session.launch.get("tool") != "spawn_agent":
                raise RuntimeError("native spawn requires a recorded Codex spawn launch")
            # Codex uses its default role when agent_type is omitted.
            spawn_input = {"agent_type": "default", **spawn_input}
            mismatches = [
                field for field in (
                    "task_name", "agent_type", "fork_turns",
                    "model", "reasoning_effort", "service_tier",
                )
                if spawn_input.get(field) != session.launch.get(field)
            ]
            if mismatches:
                raise RuntimeError("native launch contract mismatch: " + ", ".join(mismatches))
        run = self.source.read_run(session.run_id)
        require_worker_iteration_state(run, "wake native candidate")
        record = self.source.read_candidate(session.run_id, session.candidate_id)
        self.validate_candidate(session.run_id, session.candidate_id)
        if record.allocation_eligibility != "eligible":
            raise RuntimeError("cannot wake a retired native candidate")
        deadline = outer_deadline_epoch(self.context.outer_deadline(session.run_id))
        if deadline is not None and now >= deadline:
            raise RuntimeError("cannot wake a native candidate after its Goal deadline")
        frozen = self.source.read_frozen_spec(run.frozen_spec_id)
        if frozen.spec.strategy.orchestration_mode == "adaptive_search" and len(record.iterations) >= 2:
            iteration = record.iterations[-1].iteration
            barrier = confirmation_execution_barrier(
                self.source.load_schedule_task(session.run_id, session.candidate_id, iteration - 1),
                self.source.load_schedule_task(session.run_id, session.candidate_id, iteration),
            )
            if barrier:
                raise RuntimeError(f"cannot wake native candidate: {barrier}")
        return session

    def native_file_write_boundary(
        self, agent_session_id: str, *, now: float,
    ) -> CandidateFileWriteBoundary:
        session = self.require_native_execution(agent_session_id, now=now)
        run = self.source.read_run(session.run_id)
        frozen = self.source.read_frozen_spec(run.frozen_spec_id)
        record = self.source.read_candidate(session.run_id, session.candidate_id)
        return CandidateFileWriteBoundary(
            workspace=session.workspace,
            denied_files=tuple(dict.fromkeys([
                *record.task.denied_files, *frozen.verifier_hashes, ".git",
            ])),
        )

    def fenced_session(self, agent_session_id: str) -> AgentSessionRecord | None:
        session = self.source.read_agent_session(agent_session_id)
        candidate = self.source.read_candidate(session.run_id, session.candidate_id)
        return (
            session
            if session.execution_generation != candidate.execution_generation
            else None
        )

    def discover_sessions(self) -> list[AgentSessionRecord]:
        return [
            session
            for identity in self.source.read_agent_session_ids()
            if (session := self.find_session(identity)) is not None
        ]

    def recovery(self, run_id: str, candidate_id: str) -> CandidateRecovery | None:
        return self.source.read_candidate(run_id, candidate_id).recovery

    def verifier_infrastructure_report(
        self, run_id: str, candidate_id: str
    ) -> dict[str, Any] | None:
        report = self.source.read_candidate(run_id, candidate_id).score_report
        if report is not None and any(
            result.failure_class == "VerifierWorkspaceSideEffect"
            or result.metrics.get("infrastructure_failure") is True
            or result.metrics.get("candidate_action") == "stop_and_report"
            for result in report.verifier_results
        ):
            return report.model_dump(mode="json")
        return None

    def verifier_snapshot(self, run_id: str, candidate_id: str) -> dict[str, Any]:
        empty = {
            "count": 0,
            "best_iteration": None,
            "best_score": None,
            "best_git_head": None,
        }
        try:
            record = self.source.read_candidate(run_id, candidate_id)
        except (OSError, ValueError):
            return empty
        valid = [
            item
            for item in record.iterations
            if item.process_passed is True
            and not item.touched_denied_files
            and not item.changed_outside_allowed
            and item.score is not None
        ]
        direction = "maximize"
        try:
            run = self.source.read_run(run_id)
            direction = self.source.read_frozen_spec(
                run.frozen_spec_id
            ).spec.metric_direction
        except (OSError, ValueError):
            pass
        chooser = min if direction == "minimize" else max
        best = chooser(valid, key=lambda item: float(item.score), default=None)
        return {
            "count": len(record.iterations),
            "best_iteration": best.iteration if best else None,
            "best_score": best.score if best else None,
            "best_git_head": best.git_head if best else None,
        }
