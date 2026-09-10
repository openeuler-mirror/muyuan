from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Literal

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.paths import DEFAULT_RUNTIME_ROOT
from goal_plus.pi_pool import (
    close_pi_search_pool,
    continue_pi_search_pool,
    open_pi_search_pool,
    snapshot_pi_search_pool,
    wait_any_pi_search_pool,
)
from goal_plus.runtime import FileSearchRuntime
from goal_plus.infrastructure.state.files import exclusive_file_lock
from goal_plus.infrastructure.api_debug import log_api_response_size
from goal_plus.projections.pool_tools import pool_close_receipt, pool_tool_summary
from goal_plus.thinkthread_pool import (
    close_pool as close_thinkthread_search_pool,
)
from goal_plus.thinkthread_pool import (
    continue_thinkthread_search_pool,
    open_thinkthread_search_pool,
    snapshot_thinkthread_search_pool,
)
from goal_plus.thinkthread_pool import (
    wait_any as wait_any_thinkthread_search_pool,
)
from goal_plus.tools import (
    GoalPlusTools,
    SearchTools,
    ToolCallerRole,
    require_tool_role,
)

SEARCH_TOOL_NAMES = {
    "goal_plus_list_models",
    "search_freeze_spec",
    "search_create",
    "search_invalidate_run",
    "search_status",
    "search_recovery_status",
    "search_prepare_recovery",
    "search_recover_candidate",
    "search_list_history",
    "search_plan_next",
    "search_start_batch",
    "search_start_agent_session",
    "search_redispatch_candidate",
    "search_bind_agent_handle",
    "search_continue_agent_session",
    "search_get_agent_context",
    "search_get_global_evidence",
    "search_reference_open",
    "search_stage_shared_tool",
    "search_copy_shared_tool",
    "search_get_evidence_detail",
    "search_get_agent_observability",
    "search_run_verifier",
    "search_list_iterations",
    "search_list_allocation_decisions",
    "search_apply_allocation_decision",
    "search_select",
    "search_report",
    "search_promote",
    "search_apply_promotion",
}

GOAL_PLUS_TOOL_NAMES = {
    "goal_plus_create",
    "goal_plus_status",
    "goal_plus_history",
    "goal_plus_update_goal",
    "goal_plus_record_triage",
    "goal_plus_record_work_event",
    "goal_plus_save_spec_draft",
    "goal_plus_link_search_run",
    "goal_plus_record_search_result",
    "goal_plus_prepare_final_check",
    "goal_plus_submit_final_check",
    "goal_plus_set_status",
    "goal_plus_gate",
}

PI_HOST_COMMAND_NAMES = {
    "goal_plus_host_start",
    "goal_plus_host_edit",
    "goal_plus_host_control",
    "goal_plus_host_session_event",
    "goal_plus_host_kick_internal_agents",
}


def _pi_search_pool_open_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(
        run_id: str,
        candidate_ids: list[str] | None = None,
        worker_budgets: dict[str, dict[str, Any]] | None = None,
        final_verify: bool = True,
        max_parallel: int | None = None,
        execution_scope_id: str | None = None,
    ) -> dict[str, Any]:
        scope_args = (
            {"execution_scope_id": execution_scope_id}
            if execution_scope_id is not None
            else {}
        )
        return pool_tool_summary(open_pi_search_pool(
            root_dir=root_dir,
            run_id=run_id,
            candidate_ids=candidate_ids,
            worker_budgets=worker_budgets,
            final_verify=final_verify,
            max_parallel=max_parallel,
            **scope_args,
        ))

    return call


def _pi_search_pool_wait_any_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(pool_id: str, timeout_seconds: float = 30) -> dict[str, Any]:
        return pool_tool_summary(wait_any_pi_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            timeout_seconds=timeout_seconds,
        ))

    return call


def _pi_search_pool_snapshot_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(
        pool_id: str | None = None,
        run_id: str | None = None,
        detail: bool = False,
    ) -> dict[str, Any]:
        if type(detail) is not bool:
            raise ValueError("detail must be a boolean")
        snapshot = snapshot_pi_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            run_id=run_id,
        )
        return snapshot if detail else pool_tool_summary(snapshot)

    return call


def _pi_search_pool_continue_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(
        pool_id: str,
        candidate_id: str,
        worker_budget: dict[str, Any] | None = None,
        final_verify: bool = True,
    ) -> dict[str, Any]:
        return continue_pi_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            candidate_id=candidate_id,
            worker_budget=worker_budget,
            final_verify=final_verify,
        )

    return call


def _pi_search_pool_close_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(
        pool_id: str,
        mode: str = "drain",
        timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        return pool_close_receipt(close_pi_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            mode=mode,  # type: ignore[arg-type]
            timeout_seconds=timeout_seconds,
        ))

    return call


def _thinkthread_search_pool_open_tool(
    root_dir: Path | str,
) -> Callable[..., dict[str, Any]]:
    def call(
        run_id: str,
        candidate_ids: list[str] | None = None,
        worker_budgets: dict[str, dict[str, Any]] | None = None,
        max_parallel: int | None = None,
        execution_scope_id: str | None = None,
    ) -> dict[str, Any]:
        scope_args = (
            {"execution_scope_id": execution_scope_id}
            if execution_scope_id is not None
            else {}
        )
        return open_thinkthread_search_pool(
            root_dir=root_dir,
            run_id=run_id,
            candidate_ids=candidate_ids,
            worker_budgets=worker_budgets,
            max_parallel=max_parallel,
            **scope_args,
        )

    return call


def _thinkthread_search_pool_wait_any_tool(
    root_dir: Path | str,
) -> Callable[..., dict[str, Any]]:
    def call(pool_id: str, timeout_seconds: float = 30) -> dict[str, Any]:
        return wait_any_thinkthread_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            timeout_seconds=timeout_seconds,
        )

    return call


def _thinkthread_search_pool_snapshot_tool(
    root_dir: Path | str,
) -> Callable[..., dict[str, Any]]:
    def call(
        pool_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        return snapshot_thinkthread_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            run_id=run_id,
        )

    return call


def _thinkthread_search_pool_continue_tool(
    root_dir: Path | str,
) -> Callable[..., dict[str, Any]]:
    def call(
        pool_id: str,
        candidate_id: str,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return continue_thinkthread_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            candidate_id=candidate_id,
            worker_budget=worker_budget,
        )

    return call


def _thinkthread_search_pool_close_tool(
    root_dir: Path | str,
) -> Callable[..., dict[str, Any]]:
    def call(
        pool_id: str,
        mode: str = "drain",
        timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        return close_thinkthread_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            mode=mode,  # type: ignore[arg-type]
            timeout_seconds=timeout_seconds,
        )

    return call


def _pi_goal_plus_finalize_reports_tool(
    root_dir: Path | str,
) -> Callable[..., dict[str, Any]]:
    def call(
        goal_plus_id: str,
        usage: dict[str, Any],
        started_at: str | None = None,
    ) -> dict[str, Any]:
        goals = FileGoalPlusRuntime(root_dir)
        record = goals.status(goal_plus_id)
        if not goals.is_terminal(record):
            raise RuntimeError("cannot finalize reports before Goal Plus is terminal")
        goals.record_orchestrator_usage(
            goal_plus_id,
            host="pi",
            usage=usage,
            started_at=started_at,
        )
        search = FileSearchRuntime(root_dir)
        reports = [
            str(search.report(task.run_id))
            for task in record.search_tasks
            if task.report_path
            or (search.root_dir / "runs" / task.run_id / "report.md").exists()
        ]
        return {"goal_plus_id": goal_plus_id, "reports": reports}

    return call


def _host_execution_scope_open_tool(
    root_dir: Path | str,
) -> Callable[..., dict[str, Any]]:
    def call(
        host: str,
        owner_session_id: str,
        owner_pid: int,
        run_id: str | None = None,
        native_agent_config: dict[str, Any] | None = None,
        goal_plus_id: str | None = None,
        native_session_id: str | None = None,
    ) -> dict[str, Any]:
        from goal_plus.host_scope import ensure_execution_scope
        from goal_plus.ports.scope import ScopeAttachment

        if host not in {"codex", "pi"}:
            raise ValueError("execution scope host must be codex or pi")
        activation_id = None
        if goal_plus_id is not None:
            attachment = ScopeAttachment.from_id(run_id) if run_id is not None else None
            record = FileGoalPlusRuntime(root_dir).authorization.require_scope_attachment(goal_plus_id, attachment)
            binding = record.active_session
            if binding is None or binding.host != ("pi-rpc" if host == "pi" else "codex") or (
                binding.session_id != native_session_id or binding.state in {"detached", "stale"}
            ):
                raise RuntimeError("MainSessionMismatch: execution scope requires the active native session")
            activation_id = f"{record.goal_plus_id}:{record.control.version}"
        return ensure_execution_scope(
            root_dir=root_dir,
            host=host,  # type: ignore[arg-type]
            owner_session_id=owner_session_id,
            owner_pid=owner_pid,
            run_id=run_id,
            native_agent_config=native_agent_config,
            activation_id=activation_id,
        )

    return call


def _host_execution_scope_close_tool(
    root_dir: Path | str,
) -> Callable[..., dict[str, Any]]:
    def call(
        scope_id: str,
        reason: str,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        from goal_plus.host_scope import close_execution_scope

        return close_execution_scope(
            root_dir=root_dir,
            scope_id=scope_id,
            reason=reason,
            timeout_seconds=timeout_seconds,
        )

    return call


def _host_execution_scope_close_run_tool(
    root_dir: Path | str,
) -> Callable[..., dict[str, Any]]:
    def call(
        scope_id: str,
        run_id: str,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        from goal_plus.host_scope import close_run_candidates

        return close_run_candidates(
            root_dir=root_dir,
            scope_id=scope_id,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
        )

    return call


def _host_execution_scope_interrupt_allocation_tool(
    root_dir: Path | str,
) -> Callable[..., dict[str, Any]]:
    def call(
        scope_id: str,
        run_id: str,
        decision_id: str,
    ) -> dict[str, Any]:
        from goal_plus.host_scope import interrupt_allocation_candidates

        return interrupt_allocation_candidates(
            root_dir=root_dir,
            scope_id=scope_id,
            run_id=run_id,
            decision_id=decision_id,
        )

    return call


def _host_execution_scope_snapshot_tool(
    root_dir: Path | str,
) -> Callable[..., dict[str, Any]]:
    def call(scope_id: str) -> dict[str, Any]:
        from goal_plus.host_scope import execution_scope_snapshot

        return execution_scope_snapshot(root_dir=root_dir, scope_id=scope_id)

    return call


def _registry(root_dir: Path | str) -> dict[str, Callable[..., Any]]:
    goal_runtime = FileGoalPlusRuntime(root_dir)
    search_tools = SearchTools(FileSearchRuntime(root_dir), goal_runtime)
    goal_tools = GoalPlusTools(goal_runtime)
    tools: dict[str, Callable[..., Any]] = {}
    for name in SEARCH_TOOL_NAMES:
        tools[name] = getattr(search_tools, name)
    tools["search_main_agent_notifications"] = (
        search_tools.search_main_agent_notifications
    )
    tools["search_ack_main_agent_notification"] = (
        search_tools.search_ack_main_agent_notification
    )
    for name in GOAL_PLUS_TOOL_NAMES:
        tools[name] = getattr(goal_tools, name)
    tools["pi_search_pool_open"] = _pi_search_pool_open_tool(root_dir)
    tools["pi_search_pool_wait_any"] = _pi_search_pool_wait_any_tool(root_dir)
    tools["pi_search_pool_snapshot"] = _pi_search_pool_snapshot_tool(root_dir)
    tools["pi_search_pool_continue"] = _pi_search_pool_continue_tool(root_dir)
    tools["pi_search_pool_close"] = _pi_search_pool_close_tool(root_dir)
    tools["thinkthread_search_pool_open"] = _thinkthread_search_pool_open_tool(
        root_dir
    )
    tools["thinkthread_search_pool_wait_any"] = (
        _thinkthread_search_pool_wait_any_tool(root_dir)
    )
    tools["thinkthread_search_pool_snapshot"] = (
        _thinkthread_search_pool_snapshot_tool(root_dir)
    )
    tools["thinkthread_search_pool_continue"] = (
        _thinkthread_search_pool_continue_tool(root_dir)
    )
    tools["thinkthread_search_pool_close"] = _thinkthread_search_pool_close_tool(
        root_dir
    )
    tools["pi_goal_plus_finalize_reports"] = _pi_goal_plus_finalize_reports_tool(
        root_dir
    )
    tools["host_execution_scope_open"] = _host_execution_scope_open_tool(root_dir)
    tools["host_execution_scope_close"] = _host_execution_scope_close_tool(root_dir)
    tools["host_execution_scope_close_run"] = (
        _host_execution_scope_close_run_tool(root_dir)
    )
    tools["host_execution_scope_interrupt_allocation"] = (
        _host_execution_scope_interrupt_allocation_tool(root_dir)
    )
    tools["host_execution_scope_snapshot"] = (
        _host_execution_scope_snapshot_tool(root_dir)
    )
    tools["goal_plus_monitor_snapshot"] = search_tools.goal_plus_monitor_snapshot
    return tools


def call_pi_tool(
    root_dir: Path | str,
    tool_name: str,
    args: dict[str, Any] | None = None,
) -> Any:
    tools = _registry(root_dir)
    if tool_name not in tools:
        raise ValueError(f"unsupported pi tool: {tool_name}")
    arguments = args or {}
    role_name = os.environ.get("GOAL_PLUS_PI_ROLE", "main")
    roles: dict[str, ToolCallerRole] = {
        "main": "main",
        "worker": "candidate",
        "final-checker": "final_checker",
        "ordinary": "ordinary",
    }
    role = roles.get(role_name, "ordinary")
    requested_session = arguments.get("agent_session_id")
    require_tool_role(
        role,
        tool_name,
        caller_agent_session_id=os.environ.get("GOAL_PLUS_AGENT_SESSION_ID"),
        requested_agent_session_ids=(
            [requested_session] if isinstance(requested_session, str) else []
        ),
    )
    if tool_name.startswith("pi_search_pool_"):
        FileGoalPlusRuntime(root_dir).require_active_tool_target(tool_name, arguments)
    return tools[tool_name](**arguments)


def call_pi_host_command(
    root_dir: Path | str,
    command_name: str,
    args: dict[str, Any],
    *,
    capability_token: str,
) -> dict[str, Any]:
    """Dispatch the Pi extension's non-model host-command control path."""

    if command_name not in PI_HOST_COMMAND_NAMES:
        raise ValueError(f"unsupported Pi host command: {command_name}")
    _consume_pi_host_capability(root_dir, capability_token)
    if command_name == "goal_plus_host_kick_internal_agents":
        run_id = str(args.get("run_id") or "")
        if not run_id:
            raise ValueError("Pi internal-agent kick requires run_id")
        from goal_plus.internal_agents import kick_pi_internal_agents

        return {
            "ok": True,
            "run_id": run_id,
            **kick_pi_internal_agents(root_dir, run_id),
        }
    runtime = FileGoalPlusRuntime(root_dir)
    owner_pid = args.get("owner_pid")
    if command_name != "goal_plus_host_start" and isinstance(owner_pid, int) and not runtime.is_terminal(runtime.status(args["goal_plus_id"])):
        from goal_plus.host_recovery import claim_main_owner
        host_invocation = args.get("invocation") or {}
        claim_main_owner(
            Path(root_dir).resolve(), str(args.get("goal_plus_id") or ""), "pi-rpc",
            str(args.get("session_id") or host_invocation.get("session_id") or ""),
            owner_pid, restoring=True,
        )
    scope_owner_session_id = args.get("execution_scope_owner_session_id")
    if isinstance(scope_owner_session_id, str) and scope_owner_session_id:
        from goal_plus.host_scope import restore_execution_scope_guardians

        restore_execution_scope_guardians(root_dir=root_dir, host="pi", owner_session_id=scope_owner_session_id)
    if command_name == "goal_plus_host_session_event":
        event = args.get("event")
        delivery = args.get("delivery")
        interaction = args.get("interaction")
        if event not in {
            "session_start",
            "user_turn",
            "interrupt",
            "execution_lost",
            "continue_confirmed",
        }:
            raise ValueError("unsupported Pi host session event")
        if delivery not in {"auto", "next_user_turn"}:
            raise ValueError("unsupported Pi host session delivery")
        if interaction not in {"interactive", "headless"}:
            raise ValueError("unsupported Pi host session interaction")
        decision = runtime.handle_host_session_event(
            str(args.get("goal_plus_id") or ""),
            host="pi-rpc",
            session_id=str(args.get("session_id") or ""),
            event=event,
            delivery=delivery,
            interaction=interaction,
        )
        return decision.model_dump(mode="json")

    invocation = args.get("invocation")
    if not isinstance(invocation, dict):
        raise ValueError("Pi host command requires invocation evidence")
    if command_name == "goal_plus_host_start":
        record = runtime.start_from_host_command(
            raw_goal=str(args.get("raw_goal") or ""),
            source_path=(
                str(args["source_path"])
                if args.get("source_path") is not None
                else None
            ),
            policy=(args.get("policy") if isinstance(args.get("policy"), dict) else None),
            invocation=invocation,
        )
    elif command_name == "goal_plus_host_edit":
        record = runtime.edit_from_host_command(
            str(args.get("goal_plus_id") or ""),
            str(args.get("raw_goal") or ""),
            expected_revision=int(args.get("expected_revision") or 0),
            reason=(str(args["reason"]) if args.get("reason") is not None else None),
            invocation=invocation,
        )
    elif command_name == "goal_plus_host_control":
        return runtime.control_from_host_command(
            str(args.get("goal_plus_id") or ""), invocation=invocation,
        ).model_dump(mode="json")
    record = runtime.activate_session(
        record.goal_plus_id,
        {
            "host": "pi-rpc",
            "session_id": str(invocation.get("session_id") or ""),
            "metadata": {
                "authorized_by": "pi_extension_command",
                "host_invocation_id": invocation.get("invocation_id"),
                "native_entry_id": invocation.get("native_entry_id"),
            },
        },
    )
    if isinstance(owner_pid, int):
        from goal_plus.host_recovery import claim_main_owner
        claim_main_owner(Path(root_dir).resolve(), record.goal_plus_id, "pi-rpc", str(invocation["session_id"]), owner_pid, restoring=False)
    return record.model_dump(mode="json")


def _consume_pi_host_capability(
    root_dir: Path | str,
    capability_token: str,
) -> None:
    if re.fullmatch(r"[0-9a-f-]{36}", capability_token) is None:
        raise RuntimeError("invalid Pi host-entrypoint capability")
    root = Path(root_dir).resolve()
    capability_dir = root / "host-entrypoint" / "pi"
    capability_path = capability_dir / f"{capability_token}.json"
    with exclusive_file_lock(root / "host-entrypoint" / "pi-capability.lock"):
        try:
            payload = json.loads(capability_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "GoalPlusEntrypointRequired: missing one-time Pi extension capability"
            ) from exc
        capability_path.unlink(missing_ok=True)
        if payload.get("token") != capability_token:
            raise RuntimeError("invalid Pi host-entrypoint capability payload")
        issued_at_ms = payload.get("issued_at_ms")
        if not isinstance(issued_at_ms, (int, float)):
            raise RuntimeError("invalid Pi host-entrypoint capability timestamp")
        age_ms = int(time.time() * 1000) - int(issued_at_ms)
        if age_ms < -5_000 or age_ms > 120_000:
            raise RuntimeError("expired Pi host-entrypoint capability")


def _read_args(args_json: str | None) -> dict[str, Any]:
    raw = args_json
    if raw is None:
        raw = sys.stdin.read().strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="JSON CLI facade for Pi goal-plus extension tools."
    )
    parser.add_argument("tool", help="Tool name, e.g. search_get_agent_context")
    parser.add_argument(
        "--root",
        default=os.environ.get("GOAL_PLUS_ROOT", DEFAULT_RUNTIME_ROOT),
        help="Search runtime storage directory",
    )
    parser.add_argument(
        "--args-json",
        help="JSON object of tool arguments. Defaults to stdin.",
    )
    parser.add_argument(
        "--host-entrypoint",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--host-capability", help=argparse.SUPPRESS)
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parsed = parser.parse_args(argv)

    try:
        tool_args = _read_args(parsed.args_json)
        if parsed.host_entrypoint:
            if not parsed.host_capability:
                raise RuntimeError(
                    "GoalPlusEntrypointRequired: missing one-time Pi extension capability"
                )
            result = call_pi_host_command(
                parsed.root,
                parsed.tool,
                tool_args,
                capability_token=parsed.host_capability,
            )
        else:
            if parsed.tool in PI_HOST_COMMAND_NAMES:
                raise RuntimeError(
                    "GoalPlusEntrypointRequired: Pi host commands are available only "
                    "to the native /goal-plus extension entrypoint."
                )
            result = call_pi_tool(parsed.root, parsed.tool, tool_args)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "tool": parsed.tool},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    payload = json.dumps(
        result,
        ensure_ascii=False,
        indent=2 if parsed.pretty else None,
    )
    log_api_response_size(parsed.tool, payload, transport="pi-cli", format="json")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
