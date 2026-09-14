"""Bound native checker execution; GoalCompletion alone owns the verdict."""

from goal_plus.application.goal_final_check_launch import final_check_prompt
from goal_plus.domain.goal import GoalPlusFinalCheckerIdentity
from goal_plus.goal_session_host import NativeGoalSessionHost
from goal_plus.infrastructure.native_session_inputs import native_final_check_input
from goal_plus.mcp_client import McpSessionToolBridge
from goal_plus.projections.session_tools import invocation_receipt


# Bound one read-only verdict request; this is not a candidate exploration budget.
FINAL_CHECK_TIMEOUT_SECONDS = 300.0


class NativeFinalCheckHost(NativeGoalSessionHost):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending_checks: set[str] = set()

    def check(self, goal_plus_id: str) -> dict:
        with self._lock:
            if self._closing:
                raise RuntimeError("final checker host is closing")
            record = self.runtime.authorization.require_main_goal_session(
                goal_plus_id, agent_harness=self.agent_harness, session_id=self.caller(), execution=False,
            )
            previous = next((item for item in reversed(record.final_checks)
                             if item.goal_revision == record.goal_revision), None)
            if previous is not None and previous.check_id in self._pending_checks:
                return {"goal_plus_id": goal_plus_id, "goal_revision": record.goal_revision, "state": "running",
                        "final_check": previous.model_dump(mode="json", include={
                            "check_id", "status", "summary", "findings", "evidence",
                        }, exclude_none=True)}
            if previous is not None and previous.status != "pending":
                for native in list(self._resources):
                    if native.native_session_id == previous.checker_metadata.get("native_session_id"):
                        self.close_native(native)
            if previous is not None and previous.status == "passed":
                return {"goal_plus_id": goal_plus_id, "goal_revision": record.goal_revision,
                        "final_check": previous.model_dump(mode="json", include={
                            "check_id", "status", "summary", "findings", "evidence",
                        }, exclude_none=True)}
            record, _ = self.admit_goal(goal_plus_id, self.caller())
            prepared = self.runtime.goal_completion.prepare_final_check(
                goal_plus_id, self.agent_harness,
                expected_revision=record.goal_revision,
                expected_control_version=record.control.version,
            )
            check_id = prepared["check"]["check_id"]
            if check_id in self._pending_checks or prepared["check"].get("checker_metadata", {}).get("native_session_id"):
                return {"goal_plus_id": goal_plus_id, "goal_revision": record.goal_revision,
                        "final_check": {key: value for key, value in prepared["check"].items()
                                        if key in {"check_id", "status", "summary", "findings", "evidence"}}}
            self._pending_checks.add(check_id)
        try:
            return self.run(goal_plus_id, check_id, prepared["goal_revision"])
        except PermissionError as exc:
            if not str(exc).startswith("FinalCheckerAlreadyBound:"):
                raise
            current = self.runtime.state.read_goal(goal_plus_id)
            check = next(item for item in current.final_checks if item.check_id == check_id)
            return {"goal_plus_id": goal_plus_id, "goal_revision": prepared["goal_revision"],
                    "final_check": check.model_dump(mode="json", include={
                        "check_id", "status", "summary", "findings", "evidence",
                    }, exclude_none=True)}
        finally:
            with self._lock:
                self._pending_checks.discard(check_id)

    def _admit(self, identity: GoalPlusFinalCheckerIdentity, caller_id: str, scope_id: str | None = None):
        record, scope = self.admit_goal(identity.goal_plus_id, caller_id, scope_id)
        check = next((item for item in record.final_checks if item.check_id == identity.check_id), None)
        if (
            check is None or check.status != "pending" or check.checker_harness != self.agent_harness
            or record.goal_revision != identity.goal_revision or check.goal_revision != identity.goal_revision
        ):
            raise PermissionError("FinalCheckerAdmissionDenied: check is not the current pending request")
        return record, check, scope

    def run(self, goal_plus_id: str, check_id: str, goal_revision: int) -> dict:
        identity = GoalPlusFinalCheckerIdentity(goal_plus_id=goal_plus_id, check_id=check_id, goal_revision=goal_revision)
        caller_id = self.caller()
        with self._lock:
            if self._closing:
                raise RuntimeError("final checker host is closing")
            with self.runtime.state.lock_goal(goal_plus_id):
                record, check, scope = self._admit(identity, caller_id)
                if check.checker_metadata.get("native_session_id"):
                    raise PermissionError("FinalCheckerAlreadyBound: check already has a native session")
                if any(item.get("label") == f"final-checker:{check_id}" and item.get("status") != "finished"
                       for item in scope.get("processes", [])):
                    raise PermissionError("FinalCheckerAlreadyBound: check already has an execution resource")
                inputs = self.runtime.goal_completion.artifacts.final_check_inputs(record)
                prompt = final_check_prompt(record, check, inputs)
                config = native_final_check_input(self.root, inputs.workspace, identity, scope)
                scope_id = scope["scope_id"]
                bridge = McpSessionToolBridge(self.root, caller_role="final_checker", caller_final_check=identity)
                native = self.open_native(config, scope, goal_plus_id=goal_plus_id, label=f"final-checker:{check_id}",
                                          admit=lambda: self._admit(identity, caller_id, scope_id), tool_bridge=bridge)
            try:
                self._admit(identity, caller_id, scope_id)
                self.runtime.goal_completion.bind_native_checker(
                    identity, agent_harness=self.agent_harness, native_session_id=native.native_session_id,
                    scope_id=scope_id, control_version=record.control.version,
                )
                bridge.native_session_id = native.native_session_id
            except BaseException:
                self.close_native(native)
                raise
        try:
            self._admit(identity, caller_id, scope_id)
            result = native.run(prompt, timeout=FINAL_CHECK_TIMEOUT_SECONDS)
            current = self.runtime.state.read_goal(goal_plus_id)
            final_check = next(item for item in current.final_checks if item.check_id == check_id)
            if final_check.status == "pending":
                self.runtime.submit_final_check(
                    goal_plus_id, check_id, goal_revision, "interrupted",
                    "Native checker returned without a structured verdict.",
                    native_session_id=native.native_session_id,
                )
                current = self.runtime.state.read_goal(goal_plus_id)
                final_check = next(item for item in current.final_checks if item.check_id == check_id)
            return {"goal_plus_id": goal_plus_id, "goal_revision": goal_revision,
                    "final_check": final_check.model_dump(mode="json", include={
                        "check_id", "status", "summary", "findings", "evidence",
                    }, exclude_none=True),
                    "invocation": invocation_receipt(result, include_text=False)}
        except Exception:
            current = self.runtime.state.read_goal(goal_plus_id)
            check = next(item for item in current.final_checks if item.check_id == check_id)
            if (check.status == "pending" and current.status == "active" and current.control.state == "active"
                and current.goal_revision == goal_revision
                and check.checker_metadata.get("control_version") == current.control.version):
                self.runtime.submit_final_check(
                    goal_plus_id, check_id, goal_revision, "interrupted",
                    "Native checker execution failed before a structured verdict.",
                    native_session_id=native.native_session_id,
                )
            raise
        finally:
            self.close_native(native)
