from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from goal_plus.domain.agents import AgentHarnessKind

INTERNAL_AGENT_HARNESS_ENV = "GOAL_PLUS_INTERNAL_AGENT_HARNESS"
CODEX_MODEL_ENV = "GOAL_PLUS_CODEX_MODEL"
CODEX_REASONING_EFFORT_ENV = "GOAL_PLUS_CODEX_REASONING_EFFORT"


def inherited_host_environment(
    agent_harness: AgentHarnessKind,
    *,
    updates: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment from the native host process unchanged."""

    environment = os.environ.copy()
    inherited_host = environment.get(INTERNAL_AGENT_HARNESS_ENV)
    if inherited_host is not None and inherited_host != agent_harness:
        raise RuntimeError(
            "internal agent host inheritance mismatch: "
            f"expected {agent_harness!r}, inherited {inherited_host!r}"
        )
    if updates:
        environment.update(updates)
    environment[INTERNAL_AGENT_HARNESS_ENV] = agent_harness
    return environment


def require_inherited_host(agent_harness: AgentHarnessKind) -> None:
    inherited_host = os.environ.get(INTERNAL_AGENT_HARNESS_ENV)
    if inherited_host != agent_harness:
        raise RuntimeError(
            "internal agent launch requires matching native host inheritance: "
            f"expected {agent_harness!r}, inherited {inherited_host!r}"
        )


def kick_internal_agents(
    root_dir: Path | str,
    run_id: str,
    *,
    agent_harness: AgentHarnessKind,
) -> dict[str, bool]:
    """Kick every internal-agent queue from a trusted native host callback."""

    from goal_plus.llm_verifier import kick_verifier
    from goal_plus.evidence_annotator import kick_evidence_annotator
    from goal_plus.bootstrap import build_annotation_services, build_internal_agent_work
    from goal_plus.host_scope import close_run_internal_agents, run_accepts_internal_agent_launch

    work = build_internal_agent_work(root_dir).inspect(run_id)
    if work.agent_harness != agent_harness:
        raise RuntimeError(
            "internal agent host does not match the frozen worker host: "
            f"expected {work.agent_harness!r}, got {agent_harness!r}"
        )
    idle = {"annotation_started": False, "verifier_started": False}
    if not work.accepts_work:
        errors = close_run_internal_agents(root_dir=root_dir, run_id=run_id)
        if errors:
            raise RuntimeError("internal agent cleanup incomplete: " + "; ".join(errors))
        return idle
    if not run_accepts_internal_agent_launch(root_dir=root_dir, run_id=run_id):
        return idle
    _, admission, _, _ = build_annotation_services(root_dir)
    admission.ensure(run_id)

    return {
        "annotation_started": kick_evidence_annotator(
            root_dir,
            run_id,
            agent_harness=agent_harness,
        ),
        "verifier_started": work.verifier_enabled and kick_verifier(
            root_dir,
            run_id,
            agent_harness=agent_harness,
        ),
    }


def kick_pi_internal_agents(root_dir: Path | str, run_id: str) -> dict[str, bool]:
    """Trusted Pi callback supports its RPC and protected ThinkThread drivers."""
    from goal_plus.bootstrap import build_internal_agent_work

    work = build_internal_agent_work(root_dir).inspect(run_id)
    if work.agent_harness != "pi":
        raise RuntimeError("Pi internal agent callback does not own the frozen host")
    return kick_internal_agents(root_dir, run_id, agent_harness=work.agent_harness)
