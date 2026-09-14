"""Native Codex ownership records; only trusted host callbacks supply observations."""

from dataclasses import dataclass
from typing import Any, Literal

from goal_plus.execution.scope import assert_scope_admission
from goal_plus.ports.clock import Clock
from goal_plus.ports.scope import ScopeAgentRecord, ScopeAttachment, ScopeRecord, ScopeState


def codex_agent_for_target(scope: ScopeRecord, target: str) -> ScopeAgentRecord:
    agents = scope.get("codex_agents", [])
    exact = [
        item
        for item in agents
        if target in {item.get("agent_id"), item.get("native_agent_id")}
    ]
    matches = exact or [item for item in agents if item.get("label") == target]
    if len(matches) != 1:
        raise RuntimeError(
            f"Codex target is not uniquely owned by this execution scope: {target}"
        )
    return dict(matches[0])


def codex_agent_is_terminal(status: Any) -> bool:
    return (
        isinstance(status, str)
        and status in {"shutdown", "interrupted"}
        or (isinstance(status, dict) and ("completed" in status or "errored" in status))
    )


def active_codex_agents(
    scope: ScopeRecord,
    *,
    attachment: ScopeAttachment | None = None,
    candidate_id: str | None = None,
) -> list[ScopeAgentRecord]:
    return [
        item
        for item in scope.get("codex_agents", [])
        if item.get("status") == "active"
        and (attachment is None or item.get("run_id") == attachment.identity)
        and (candidate_id is None or item.get("candidate_id") == candidate_id)
    ]


@dataclass(frozen=True)
class CodexScopeRegistry:
    state: ScopeState
    clock: Clock

    def register(
        self,
        *,
        scope_id: str,
        agent_id: str,
        label: str,
        attachment: ScopeAttachment | None = None,
        tool_use_id: str | None = None,
        parent_session_id: str | None = None,
        candidate_id: str | None = None,
        agent_session_id: str | None = None,
        execution_generation: int | None = None,
    ) -> None:
        """Persist a native child before launch; pending registration remains a cleanup obligation."""
        if not agent_id:
            raise ValueError("agent_id is required")
        candidate_binding = (
            candidate_id,
            agent_session_id,
            execution_generation,
        )
        if any(value is not None for value in candidate_binding) and not all(
            value is not None for value in candidate_binding
        ):
            raise ValueError("Codex candidate registration requires complete identity")
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            if scope.get("host") != "codex":
                raise ValueError(f"execution scope {scope_id} is not a Codex scope")
            if scope.get("state") != "open":
                raise RuntimeError(f"execution scope {scope_id} is not open")
            if attachment is not None:
                assert_scope_admission(scope, scope_id=scope_id, attachment=attachment)
            agents = scope.setdefault("codex_agents", [])
            current = next(
                (item for item in agents if item.get("agent_id") == agent_id),
                None,
            )
            if tool_use_id:
                if not parent_session_id:
                    raise ValueError("Codex spawn requires its native parent session")
                if current is not None:
                    if current.get("spawn_tool_use_id") == tool_use_id and current.get(
                        "spawn_pending"
                    ):
                        return
                    raise RuntimeError(
                        "Codex task is already registered; spawn replay is fenced"
                    )
                if any(
                    item.get("agent_id") == agent_id
                    and item.get("parent_session_id") == parent_session_id
                    for previous in self.state.records()
                    for item in previous.get("codex_agents", [])
                ):
                    raise RuntimeError(
                        "Codex task name was already used by this native parent session"
                    )
            now = self.clock.timestamp()
            if current is None:
                agents.append(
                    {
                        "agent_id": agent_id,
                        "label": label,
                        "run_id": attachment.identity if attachment is not None else None,
                        **(
                            {
                                "candidate_id": candidate_id,
                                "agent_session_id": agent_session_id,
                                "execution_generation": execution_generation,
                            }
                            if candidate_id is not None
                            and agent_session_id is not None
                            and execution_generation is not None
                            else {}
                        ),
                        "status": "active",
                        "registered_at": now,
                        "updated_at": now,
                        **(
                            {
                                "spawn_tool_use_id": tool_use_id,
                                "spawn_pending": True,
                                "parent_session_id": parent_session_id,
                            }
                            if tool_use_id
                            else {}
                        ),
                    }
                )
            else:
                current["label"] = label
                current["run_id"] = (attachment.identity if attachment is not None else None) or current.get("run_id")
                if candidate_id is not None:
                    current["candidate_id"] = candidate_id
                if agent_session_id is not None:
                    current["agent_session_id"] = agent_session_id
                if execution_generation is not None:
                    current["execution_generation"] = execution_generation
                current["updated_at"] = now
                if current.get("status") == "pending_registration":
                    current["status"] = "active"
            self.state.write(scope)

    def complete_spawn(
        self,
        *,
        scope_id: str,
        tool_use_id: str,
        agent_id: str,
    ) -> None:
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            matches = [
                item
                for item in scope.get("codex_agents", [])
                if item.get("spawn_tool_use_id") == tool_use_id
            ]
            if len(matches) != 1:
                raise RuntimeError("Codex spawn returned without its scoped invocation")
            if scope.get("state") == "closed":
                return
            agent = matches[0]
            if agent.get("native_agent_id") not in {None, agent_id}:
                raise RuntimeError("Codex spawn returned a conflicting native identity")
            agent.update(
                native_agent_id=agent_id,
                spawn_pending=False,
                updated_at=self.clock.timestamp(),
            )
            self.state.write(scope)

    def finish(
        self,
        *,
        scope_id: str,
        agent_id: str,
        status: str = "finished",
        attachment: ScopeAttachment | None = None,
        create_if_missing: bool = False,
        tool_use_id: str | None = None,
    ) -> bool:
        """Record a native Codex child terminal observation from a host hook."""
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            if scope.get("state") == "closed":
                return False
            agents = scope.setdefault("codex_agents", [])
            exact = [
                agent
                for agent in agents
                if agent_id in {agent.get("agent_id"), agent.get("native_agent_id")}
            ]
            matches = exact or [
                agent for agent in agents if agent.get("label") == agent_id
            ]
            if len(matches) == 1:
                agent = matches[0]
                if agent.get("spawn_pending"):
                    return False
                if (
                    status == "interrupted"
                    and agent.get("spawn_tool_use_id")
                    and (
                        not tool_use_id
                        or agent.get("interrupt_observation")
                        != [tool_use_id, agent.get("wake_generation", 0)]
                    )
                ):
                    return False
                if agent.get("wake_generation", 0) and (
                    status != "interrupted"
                    or not tool_use_id
                    or agent.get("wake_pending")
                    or agent.get("interrupt_observation")
                    != [tool_use_id, agent["wake_generation"]]
                ):
                    return False
                matches[0]["status"] = status
                matches[0]["finished_at"] = self.clock.timestamp()
                matches[0]["updated_at"] = matches[0]["finished_at"]
                self.state.write(scope)
                return True
            if create_if_missing:
                now = self.clock.timestamp()
                agents.append(
                    {
                        "agent_id": agent_id,
                        "label": agent_id,
                        "run_id": attachment.identity if attachment is not None else None,
                        "status": status,
                        "registered_at": None,
                        "finished_at": now,
                        "updated_at": now,
                    }
                )
                self.state.write(scope)
                return True
        return False

    def prepare_tool(
        self,
        *,
        scope_id: str,
        tool: Literal["followup_task", "interrupt_agent", "list_agents"],
        tool_use_id: str | None,
        target: str | None = None,
    ) -> None:
        """Fence a native wake and correlate observations before invoking the host tool."""
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            if scope.get("host") != "codex":
                raise ValueError("native agent tools require a Codex execution scope")
            if tool == "list_agents":
                if not tool_use_id:
                    return
                if scope.get("codex_observation", {}).get("tool_use_id") == tool_use_id:
                    return
                scope["codex_observation"] = {
                    "tool_use_id": tool_use_id,
                    "generations": {
                        item["agent_id"]: item.get("wake_generation", 0)
                        for item in scope.get("codex_agents", [])
                        if not item.get("wake_pending")
                    },
                }
            else:
                if not target:
                    raise ValueError(f"{tool} requires a native target")
                selected = codex_agent_for_target(scope, target)
                agent = next(
                    item
                    for item in scope["codex_agents"]
                    if item["agent_id"] == selected["agent_id"]
                )
                if tool == "interrupt_agent":
                    if tool_use_id:
                        agent["interrupt_observation"] = [
                            tool_use_id,
                            agent.get("wake_generation", 0),
                        ]
                else:
                    if not tool_use_id:
                        raise RuntimeError(
                            "Codex followup requires the native tool_use_id"
                        )
                    run_id = agent.get("run_id")
                    if not run_id:
                        raise RuntimeError(
                            "Codex followup requires a bound Goal or Search run"
                        )
                    assert_scope_admission(scope, scope_id=scope_id, attachment=ScopeAttachment.from_id(run_id))
                    if agent.get("wake_tool_use_id") == tool_use_id:
                        if agent.get("wake_pending"):
                            return
                        raise RuntimeError(
                            "Codex followup invocation already returned; replay is fenced"
                        )
                    if agent.get("wake_pending") or agent.get("spawn_pending"):
                        raise RuntimeError(
                            "Codex followup outcome is unknown; new wake is fenced"
                        )
                    agent.update(
                        status="active",
                        wake_generation=agent.get("wake_generation", 0) + 1,
                        wake_tool_use_id=tool_use_id,
                        wake_pending=True,
                        updated_at=self.clock.timestamp(),
                    )
                    agent.pop("finished_at", None)
            self.state.write(scope)

    def complete_followup(
        self,
        *,
        scope_id: str,
        target: str,
        tool_use_id: str | None,
    ) -> None:
        """A returned tool is observable; success or quiescence still needs native evidence."""
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            selected = codex_agent_for_target(scope, target)
            for agent in scope["codex_agents"]:
                if (
                    agent["agent_id"] == selected["agent_id"]
                    and tool_use_id
                    and agent.get("wake_tool_use_id") == tool_use_id
                ):
                    agent["wake_pending"] = False
                    self.state.write(scope)
                    return
            raise RuntimeError(
                "Codex followup returned without its current scoped invocation"
            )

    def record_observation(
        self,
        *,
        scope_id: str,
        tool_use_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply only a registry read begun after a wake returned, for the same local generation."""
        accepted = []
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            if scope.get("state") == "closed":
                return {"agents": []}
            observation = scope.get("codex_observation", {})
            if not tool_use_id or observation.get("tool_use_id") != tool_use_id:
                return {"agents": []}
            records = payload.get("agents")
            if not isinstance(records, list):
                return {"agents": []}
            for item in scope.get("codex_agents", []):
                if item.get("wake_pending") or observation["generations"].get(
                    item["agent_id"]
                ) != item.get("wake_generation", 0):
                    continue
                matches = [
                    entry
                    for entry in records
                    if isinstance(entry, dict)
                    and entry.get("agent_name") == item["agent_id"]
                ]
                if len(matches) != 1:
                    continue
                entry = matches[0]
                terminal = codex_agent_is_terminal(entry.get("agent_status"))
                if terminal:
                    item.update(
                        status="finished",
                        finished_at=self.clock.timestamp(),
                        updated_at=self.clock.timestamp(),
                    )
                elif entry.get("agent_status") == "running":
                    item.update(status="active", updated_at=self.clock.timestamp())
                    item.pop("finished_at", None)
                else:
                    continue
                item["spawn_pending"] = False
                accepted.append(entry)
            observation["generations"] = {}
            self.state.write(scope)
        return {"agents": accepted}

    def finish_after_owner_exit(self, *, scope_id: str) -> None:
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            now = self.clock.timestamp()
            for agent in active_codex_agents(scope):
                agent["status"] = "owner_process_exited"
                agent["finished_at"] = now
                agent["updated_at"] = now
            self.state.write(scope)

    def for_agent(
        self,
        *,
        agent_id: str,
        parent_session_id: str | None = None,
    ) -> ScopeRecord | None:
        """Resolve exact ownership; a reused task label never selects the newest scope."""
        matches = [
            scope
            for scope in self.state.records()
            if (
                scope.get("host") == "codex"
                and any(
                    agent_id
                    in {
                        item.get("agent_id"),
                        item.get("native_agent_id"),
                        item.get("label"),
                    }
                    and (
                        parent_session_id is None
                        or item.get("parent_session_id", scope.get("owner_session_id"))
                        == parent_session_id
                    )
                    for item in scope.get("codex_agents", [])
                    if isinstance(item, dict)
                )
            )
        ]
        if len(matches) > 1:
            raise RuntimeError(
                "Codex task identity is ambiguous across execution scopes"
            )
        return matches[0] if matches else None

    def for_invocation(
        self,
        *,
        tool_use_id: str | None,
    ) -> ScopeRecord | None:
        if not tool_use_id:
            return None
        matches = [
            scope
            for scope in self.state.records()
            if scope.get("host") == "codex"
            and any(
                tool_use_id
                in {item.get("spawn_tool_use_id"), item.get("wake_tool_use_id")}
                or item.get("interrupt_observation", [None])[0] == tool_use_id
                for item in scope.get("codex_agents", [])
            )
        ]
        if len(matches) > 1:
            raise RuntimeError(
                "Codex tool invocation is ambiguous across execution scopes"
            )
        return matches[0] if matches else None
