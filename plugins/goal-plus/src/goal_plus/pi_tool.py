from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.paths import DEFAULT_RUNTIME_ROOT
from goal_plus.pi_pool import (
    close_pi_search_pool,
    continue_pi_search_pool,
    open_pi_search_pool,
    snapshot_pi_search_pool,
    wait_any_pi_search_pool,
)
from goal_plus.runtime import FileSearchRuntime, exclusive_file_lock
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
    "goal_plus_host_session_event",
}


def _pi_search_pool_open_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(
        run_id: str,
        candidate_ids: list[str] | None = None,
        worker_budgets: dict[str, dict[str, Any]] | None = None,
        final_verify: bool = True,
        max_parallel: int | None = None,
    ) -> dict[str, Any]:
        return open_pi_search_pool(
            root_dir=root_dir,
            run_id=run_id,
            candidate_ids=candidate_ids,
            worker_budgets=worker_budgets,
            final_verify=final_verify,
            max_parallel=max_parallel,
        )

    return call


def _pi_search_pool_wait_any_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(pool_id: str, timeout_seconds: float = 30) -> dict[str, Any]:
        return wait_any_pi_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            timeout_seconds=timeout_seconds,
        )

    return call


def _pi_search_pool_snapshot_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(
        pool_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        return snapshot_pi_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            run_id=run_id,
        )

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
        return close_pi_search_pool(
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
        if record.status not in {"blocked", "complete", "abandoned"}:
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


def _registry(root_dir: Path | str) -> dict[str, Callable[..., Any]]:
    goal_runtime = FileGoalPlusRuntime(root_dir)
    search_tools = SearchTools(FileSearchRuntime(root_dir), goal_runtime)
    goal_tools = GoalPlusTools(goal_runtime)
    tools: dict[str, Callable[..., Any]] = {}
    for name in SEARCH_TOOL_NAMES:
        tools[name] = getattr(search_tools, name)
    for name in GOAL_PLUS_TOOL_NAMES:
        tools[name] = getattr(goal_tools, name)
    tools["pi_search_pool_open"] = _pi_search_pool_open_tool(root_dir)
    tools["pi_search_pool_wait_any"] = _pi_search_pool_wait_any_tool(root_dir)
    tools["pi_search_pool_snapshot"] = _pi_search_pool_snapshot_tool(root_dir)
    tools["pi_search_pool_continue"] = _pi_search_pool_continue_tool(root_dir)
    tools["pi_search_pool_close"] = _pi_search_pool_close_tool(root_dir)
    tools["pi_goal_plus_finalize_reports"] = _pi_goal_plus_finalize_reports_tool(
        root_dir
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
    runtime = FileGoalPlusRuntime(root_dir)
    if command_name == "goal_plus_host_session_event":
        event = args.get("event")
        delivery = args.get("delivery")
        interaction = args.get("interaction")
        if event not in {
            "session_start",
            "user_turn",
            "interrupt",
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

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if parsed.pretty else None,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
