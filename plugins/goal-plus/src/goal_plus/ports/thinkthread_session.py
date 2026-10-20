"""Host-owned retained Child inputs and operational persistence callbacks."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ContextManager

from goal_plus.domain.agents import AgentHarnessKind
from goal_plus.domain.tool_catalog import CandidateToolCapabilities
from goal_plus.ports.thinkthread import ChildBinding


@dataclass(frozen=True)
class ThinkThreadSessionInput:
    root_id: str
    baseline_snapshot_id: str
    model: dict[str, str] | None = None
    # Pre-Codex resource journals did not record a harness; they are all Pi.
    agent_harness: AgentHarnessKind = "pi"
    fork: bool = False
    tool_capabilities: CandidateToolCapabilities | None = None


@dataclass(frozen=True)
class ThinkThreadSessionHooks:
    admission: Callable[[], ContextManager[None]]
    persist: Callable[[dict[str, Any]], None]
    bind: Callable[[ChildBinding], None]
    receive: Callable[[dict[str, Any]], None]
    settle: Callable[[dict[str, Any]], str]
