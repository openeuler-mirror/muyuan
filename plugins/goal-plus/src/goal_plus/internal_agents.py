from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from goal_plus.domain.agents import AgentHostKind

INTERNAL_AGENT_HOST_ENV = "GOAL_PLUS_INTERNAL_AGENT_HOST"
CODEX_MODEL_ENV = "GOAL_PLUS_CODEX_MODEL"
CODEX_REASONING_EFFORT_ENV = "GOAL_PLUS_CODEX_REASONING_EFFORT"


def inherited_host_environment(
    host: AgentHostKind,
    *,
    updates: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment from the native host process unchanged."""

    environment = os.environ.copy()
    inherited_host = environment.get(INTERNAL_AGENT_HOST_ENV)
    if inherited_host is not None and inherited_host != host:
        raise RuntimeError(
            "internal agent host inheritance mismatch: "
            f"expected {host!r}, inherited {inherited_host!r}"
        )
    if updates:
        environment.update(updates)
    environment[INTERNAL_AGENT_HOST_ENV] = host
    return environment


def require_inherited_host(host: AgentHostKind) -> None:
    inherited_host = os.environ.get(INTERNAL_AGENT_HOST_ENV)
    if inherited_host != host:
        raise RuntimeError(
            "internal agent launch requires matching native host inheritance: "
            f"expected {host!r}, inherited {inherited_host!r}"
        )


def kick_internal_agents(
    root_dir: Path | str,
    run_id: str,
    *,
    host: AgentHostKind,
) -> dict[str, bool]:
    """Kick every internal-agent queue from a trusted native host callback."""

    from goal_plus.llm_verifier import kick_verifier
    from goal_plus.evidence_annotator import kick_evidence_annotator
    from goal_plus.bootstrap import build_annotation_services, build_internal_agent_work
    from goal_plus.host_scope import close_run_internal_agents, run_accepts_internal_agent_launch

    work = build_internal_agent_work(root_dir).inspect(run_id)
    if work.host != host:
        raise RuntimeError(
            "internal agent host does not match the frozen worker host: "
            f"expected {work.host!r}, got {host!r}"
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
            host=host,
        ),
        "verifier_started": work.verifier_enabled and kick_verifier(
            root_dir,
            run_id,
            host=host,
        ),
    }


def kick_pi_internal_agents(root_dir: Path | str, run_id: str) -> dict[str, bool]:
    """Trusted Pi callback supports its RPC and protected ThinkThread drivers."""
    from goal_plus.bootstrap import build_internal_agent_work

    work = build_internal_agent_work(root_dir).inspect(run_id)
    if work.host not in {"pi-rpc", "thinkthread"}:
        raise RuntimeError("Pi internal agent callback does not own the frozen host")
    return kick_internal_agents(root_dir, run_id, host=work.host)
