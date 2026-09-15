"""Production session controls for workspace_provider scenarios with deterministic Main input."""

import hashlib

from goal_plus.domain.goal import GoalPlusHostCommandInvocation
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import close_execution_scope, ensure_execution_scope
from goal_plus.infrastructure.clock import utc_timestamp
from goal_plus.session_host import NativeCandidateHost


class CandidateSessions:
    def __init__(self, runtime, run_id, config):
        self.runtime, self.run_id = runtime, run_id
        self.goals = FileGoalPlusRuntime(runtime.root_dir)
        run = runtime.search_run.state.read_run(run_id)
        frozen = runtime.search_run.state.read_frozen_spec(run.frozen_spec_id)
        caller = config["native_session_id"]
        text = "Verify the assigned isolated candidates and review their exact artifacts."
        goal = self.goals.start_from_host_command(text, source_path=frozen.spec.source_path,
            invocation=GoalPlusHostCommandInvocation(
                agent_harness="pi", transport="pi_extension_command", command="/goal-plus", action="start",
                session_id=caller, native_entry_id=run_id, invocation_id="pi:st:" + run_id,
                raw_input_sha256=hashlib.sha256(("/goal-plus " + text).encode()).hexdigest(),
                invoked_at=utc_timestamp(),
            ))
        self.goal = self.goals.activate_session(goal.goal_plus_id, {
            "agent_harness": "pi", "session_id": caller, "attached_at": goal.created_at,
            "last_seen_at": goal.updated_at,
        })
        self.goals.link_search_run(goal.goal_plus_id, run.frozen_spec_id, run_id)
        self.scope = ensure_execution_scope(root_dir=runtime.root_dir, agent_harness="pi",
            owner_session_id=caller,  run_id=run_id,
             activation_id=f"{goal.goal_plus_id}:{goal.control.version}")
        self.agent_harness = NativeCandidateHost(runtime.root_dir, agent_harness="pi", caller=lambda: caller)
        self.sessions = {record.candidate_id: runtime.start_agent_session(run_id, record.candidate_id)
                         for record in runtime.candidate_creation.state.read_candidates(run_id)}
        self.controls = {}

    def start(self, candidate_id, *, call_id="1", prompt="Complete your assigned candidate attempt."):
        session = self.sessions[candidate_id]
        if candidate_id not in self.controls:
            self.agent_harness.open(session.agent_session_id)
            self.controls[candidate_id] = self.agent_harness(session.agent_session_id, "wake")
        self.controls[candidate_id].wake(prompt, call_id=call_id, timeout=240)

    def start_all(self):
        try:
            for candidate_id in self.sessions:
                self.start(candidate_id)
        except BaseException:
            self.close()
            raise

    def wait(self, candidate_id, *, call_id="1", timeout=300):
        snapshot = self.controls[candidate_id].wait(call_id, timeout=timeout)
        assert snapshot.state == "completed", snapshot
        return snapshot.result

    def close_candidates(self):
        self.agent_harness.shutdown()

    def close(self):
        try:
            self.close_candidates()
        finally:
            closed = close_execution_scope(root_dir=self.runtime.root_dir,
                scope_id=self.scope["scope_id"], reason="TT workspace_provider scenario finished")
        assert closed["state"] == "closed", closed
        return closed


def complete_scenario_goal(runtime, run_id, manifest):
    goals = FileGoalPlusRuntime(runtime.root_dir)
    goal_id = goals.authorization.run_owner(run_id)
    run = runtime.search_run.state.read_run(run_id)
    goals.record_search_result(goal_id, run_id, selected_candidate_id=run.selected_candidate_id,
                               promotion_artifact_path=str(manifest))
    goals.set_status(goal_id, "complete")
