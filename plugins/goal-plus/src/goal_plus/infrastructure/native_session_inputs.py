"""Translate admitted role inputs and the Main's frozen facts to native input."""

import os
from collections.abc import Mapping
from pathlib import Path

from goal_plus.codex_hook_input import CODEX_CANDIDATE_SESSION_ENV, CODEX_ORDINARY_ROLE_ENV
from goal_plus.domain.search import AgentSessionRecord
from goal_plus.domain.goal import GoalPlusFinalCheckerIdentity
from goal_plus.infrastructure.final_check_binding import FINAL_CHECK_IDENTITY_ENV
from goal_plus.execution.scope import normalize_native_agent_config
from goal_plus.paths import host_assets_root
from goal_plus.ports.scope import ScopeRecord
from goal_plus.ports.session import NativeSessionInput, SessionProcessInput


def _native_environment(root: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GOAL_PLUS_") and key != "CODEX_THREAD_ID"
    }
    # Installation identity is needed by the Pi extension's trusted Python launcher.
    for key in ("GOAL_PLUS_PI_DEV_ROOT", "GOAL_PLUS_PYTHON"):
        if key in os.environ:
            environment[key] = os.environ[key]
    environment["GOAL_PLUS_ROOT"] = str(root)
    return environment


def native_candidate_input(
    root: Path, session: AgentSessionRecord, scope: ScopeRecord,
    *, process_environment: Mapping[str, str], resume: bool = False,
) -> NativeSessionInput:
    agent_harness = session.agent_harness
    if session.runtime_provider != "direct" or scope.get("agent_harness") != agent_harness:
        raise PermissionError("native candidate host does not match its execution scope")
    main_config = normalize_native_agent_config(agent_harness, scope.get("native_agent_config"))
    if main_config is None:
        raise RuntimeError("native candidate launch requires the Main model snapshot")
    launch = session.launch
    native_session_id = session.session_handle.external_id if resume else None
    if resume and not native_session_id:
        raise ValueError("candidate continuation requires an exact native session handle")
    dispatches = session.session_handle.metadata.get("dispatches") or []
    metrics_baseline = dict(dispatches[-1].get("metrics_baseline") or {}) if resume and dispatches else {}
    environment = {**_native_environment(root), **process_environment}
    if agent_harness == "codex":
        environment["GOAL_PLUS_SEARCH_ROOT"] = str(root)
        environment[CODEX_CANDIDATE_SESSION_ENV] = session.agent_session_id
        configuration = {"sandbox_mode": "workspace-write"}
        if launch.get("service_tier"):
            configuration["service_tier"] = launch["service_tier"]
        return NativeSessionInput(
            agent_harness=agent_harness,
            process=SessionProcessInput(("codex", "app-server", "--stdio"), session.workspace, environment),
            model=launch.get("model") or main_config["model"],
            reasoning_effort=launch.get("reasoning_effort") or main_config.get("reasoning_effort"),
            native_session_id=native_session_id,
            configuration=configuration,
            metrics_baseline=metrics_baseline,
        )
    extension = host_assets_root() / "assets/pi/extensions/goal-plus.ts"
    if not extension.is_file():
        raise FileNotFoundError(f"Pi extension not found: {extension}")
    session_dir = root / "host-sessions" / "pi"
    session_dir.mkdir(parents=True, exist_ok=True)
    environment.update({
        "GOAL_PLUS_PI_ROLE": "worker",
        "GOAL_PLUS_AGENT_SESSION_ID": session.agent_session_id,
    })
    return NativeSessionInput(
        agent_harness=agent_harness,
        process=SessionProcessInput((
            "pi", "--mode", "rpc", "--approve", "--no-context-files",
            "--session-dir", str(session_dir), "--session" if resume else "--session-id",
            native_session_id if resume else session.agent_session_id,
            "--no-extensions", "-e", str(extension),
        ), session.workspace, environment),
        model=launch.get("model_pattern") or main_config["model_pattern"],
        reasoning_effort=launch.get("thinking_level") or main_config.get("thinking_level"),
        native_session_id=native_session_id,
        metrics_baseline=metrics_baseline,
    )


def native_final_check_input(
    root: Path, workspace: Path, identity: GoalPlusFinalCheckerIdentity, scope: ScopeRecord,
) -> NativeSessionInput:
    agent_harness = scope["agent_harness"]
    config = normalize_native_agent_config(agent_harness, scope.get("native_agent_config"))
    if config is None:
        raise RuntimeError("native final checker requires the Main model snapshot")
    environment = _native_environment(root)
    environment[FINAL_CHECK_IDENTITY_ENV] = identity.model_dump_json()
    if agent_harness == "codex":
        environment["GOAL_PLUS_SEARCH_ROOT"] = str(root)
        return NativeSessionInput(
            agent_harness=agent_harness,
            process=SessionProcessInput(("codex", "app-server", "--stdio"), workspace, environment),
            model=config["model"], reasoning_effort=config.get("reasoning_effort"),
            configuration={"sandbox_mode": "read-only"},
        )
    extension = host_assets_root() / "assets/pi/extensions/goal-plus.ts"
    if not extension.is_file():
        raise FileNotFoundError(f"Pi extension not found: {extension}")
    environment["GOAL_PLUS_PI_ROLE"] = "final-checker"
    session_dir = root / "host-sessions" / "pi"
    session_dir.mkdir(parents=True, exist_ok=True)
    return NativeSessionInput(
        agent_harness=agent_harness,
        process=SessionProcessInput((
            "pi", "--mode", "rpc", "--approve", "--no-context-files",
            "--tools", "read,goal_plus_status,goal_plus_submit_final_check",
            "--session-dir", str(session_dir), "--session-id", identity.check_id,
            "--no-extensions", "-e", str(extension),
        ), workspace, environment),
        model=config["model_pattern"], reasoning_effort=config.get("thinking_level"),
    )


def native_work_item_input(
    root: Path, workspace: Path, session_id: str, scope: ScopeRecord, *, resume: bool = False,
) -> NativeSessionInput:
    agent_harness = scope["agent_harness"]
    config = normalize_native_agent_config(agent_harness, scope.get("native_agent_config"))
    if config is None:
        raise RuntimeError("native delegation requires the Main model snapshot")
    environment = _native_environment(root)
    if agent_harness == "codex":
        environment.update({"GOAL_PLUS_SEARCH_ROOT": str(root), CODEX_ORDINARY_ROLE_ENV: "ordinary"})
        return NativeSessionInput(
            agent_harness=agent_harness, process=SessionProcessInput(("codex", "app-server", "--stdio"), workspace, environment),
            model=config["model"], reasoning_effort=config.get("reasoning_effort"),
            native_session_id=session_id if resume else None,
            configuration={"sandbox_mode": "workspace-write"},
        )
    extension = host_assets_root() / "assets/pi/extensions/goal-plus.ts"
    if not extension.is_file():
        raise FileNotFoundError(f"Pi extension not found: {extension}")
    environment["GOAL_PLUS_PI_ROLE"] = "ordinary"
    session_dir = root / "host-sessions" / "pi"
    session_dir.mkdir(parents=True, exist_ok=True)
    return NativeSessionInput(
        agent_harness=agent_harness,
        process=SessionProcessInput((
            "pi", "--mode", "rpc", "--approve", "--no-context-files",
            "--session-dir", str(session_dir), "--session" if resume else "--session-id", session_id,
            "--no-extensions", "-e", str(extension),
        ), workspace, environment),
        model=config["model_pattern"], reasoning_effort=config.get("thinking_level"),
        native_session_id=session_id if resume else None,
    )
