"""Durable Search creation intent and revision-bound Goal association."""

from goal_plus.application.goal_search import GoalSearch
from goal_plus.domain.goal import GoalSearchCreateRequest
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.ports.goal_authorization import GoalToolTarget
from goal_plus.ports.search_run import SearchRunCreation


class GoalSearchCreate:
    def __init__(self, goals: GoalSearch, runs: SearchRunCreation) -> None:
        self.goals, self.runs = goals, runs

    def create(self, goal_plus_id: str, frozen: FrozenSpec, request_id: str,
               source_run_id: str | None = None) -> dict[str, str]:
        if not request_id.strip():
            raise ValueError("request_id is required")
        fingerprint = sha256_text(canonical_json({
            "frozen_spec_id": frozen.frozen_spec_id, "source_run_id": source_run_id,
        }))
        key = sha256_text(request_id)
        state = self.goals.state
        with state.lock_search_create(goal_plus_id, key):
            with state.lock_goal(goal_plus_id):
                goal = state.read_goal(goal_plus_id)
                request = next((item for item in goal.search_create_requests if item.request_id == request_id), None)
                if request is not None:
                    if request.fingerprint != fingerprint:
                        raise ValueError("SearchCreateConflict: request_id was used with different input")
                    if request.state == "linked":
                        return {"run_id": request.run_id, "state": "linked"}
                self.goals.authorization.require_active_target(GoalToolTarget(
                    "goal_plus_search_create", goal_plus_id=goal_plus_id, source_run_id=source_run_id,
                ))
                if frozen.goal_plus_id != goal_plus_id or frozen.goal_revision != goal.goal_revision:
                    raise RuntimeError("SearchCreateStale: FrozenSpec does not belong to the current Goal revision")
                if request is None:
                    request = GoalSearchCreateRequest(
                        request_id=request_id, fingerprint=fingerprint,
                        frozen_spec_id=frozen.frozen_spec_id, source_run_id=source_run_id,
                        run_id=self.runs.new_run_id(),
                        goal_revision=goal.goal_revision, control_version=goal.control.version,
                    )
                    goal.search_create_requests.append(request)
                    state.write_goal(goal)
                elif (request.goal_revision != goal.goal_revision
                      or request.control_version != goal.control.version):
                    raise RuntimeError("SearchCreateStale: Goal changed during Search creation")
            try:
                if request.state != "created":
                    self._mark(goal_plus_id, request_id, "creating")
                    self.runs.create_run(frozen.frozen_spec_id, source_run_id, run_id=request.run_id)
                    self._mark(goal_plus_id, request_id, "created")
                self.goals.link_search_run(
                    goal_plus_id, frozen.frozen_spec_id, request.run_id,
                    expected_revision=request.goal_revision, expected_control_version=request.control_version,
                )
                self._mark(goal_plus_id, request_id, "linked")
            except Exception as exc:
                self._mark(goal_plus_id, request_id, "failed", f"{type(exc).__name__}: {exc}")
                current = state.read_goal(goal_plus_id)
                if not any(item.run_id == request.run_id for item in current.search_tasks) and (
                    current.goal_revision != request.goal_revision or current.control.version != request.control_version
                    or current.control.state != "active" or current.status != "active"
                ):
                    self.runs.fail_creation(request.run_id)
                raise
            return {"run_id": request.run_id, "state": "linked"}

    def _mark(self, goal_plus_id, request_id, stage, error=None):
        with self.goals.state.lock_goal(goal_plus_id):
            goal = self.goals.state.read_goal(goal_plus_id)
            request = next(item for item in goal.search_create_requests if item.request_id == request_id)
            request.state, request.error = stage, error
            self.goals.state.write_goal(goal)
