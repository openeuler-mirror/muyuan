"""Pi pool tool replies expose outcomes, not completed launch/session records."""

from typing import Any


def worker_result_summary(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    payload = {key: result[key] for key in (
        "ok", "run_id", "candidate_id", "agent_session_id", "error", "failure",
        "final_score_report",
    ) if key in result}
    if isinstance(result.get("lease"), dict):
        payload["lease"] = {key: result["lease"][key] for key in (
            "satisfied", "release_reason", "dispatch_count", "verifier_runs",
            "elapsed_seconds", "max_runtime_seconds", "min_runtime_seconds",
            "min_verifier_runs", "pending_session_refresh_reason",
        ) if key in result["lease"]}
    return payload


def pool_tool_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if "pools" in payload:
        return {**payload, "pools": [pool_tool_summary(pool) for pool in payload["pools"]]}
    result = {key: payload[key] for key in (
        "ok", "pool_id", "run_id", "state", "max_parallel", "active_count", "free_slots",
        "terminal_count", "undelivered_count", "submitted", "timed_out", "error",
    ) if key in payload}
    if "events" in payload:
        result["events"] = [
            {**event, "result": worker_result_summary(event.get("result"))}
            for event in payload["events"]
        ]
    if "jobs" in payload:
        result["jobs_total"] = len(payload["jobs"])
        pending = _pending_jobs(payload["jobs"])
        if pending:
            result["jobs"] = pending
        result["details"] = {"tool": "pi_search_pool_snapshot", "detail": True}
    return result


def _pending_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest = {job["candidate_id"]: job["job_id"] for job in jobs}
    pending = []
    for job in jobs:
        result = job.get("result") or {}
        failed = (job["status"] in {"failed", "interrupted", "timed_out"} or job.get("error")
                  or result.get("error") or result.get("failure") or result.get("ok") is False)
        if (job["status"] in {"starting", "running"} or not job.get("delivered_at")
                or (failed and latest[job["candidate_id"]] == job["job_id"])):
            item = {key: job[key] for key in (
                "job_id", "candidate_id", "status", "error",
            ) if job.get(key) is not None}
            item["delivered"] = bool(job.get("delivered_at"))
            if failed:
                item["failure"] = {key: result[key] for key in ("ok", "error", "failure")
                                   if key in result}
            pending.append(item)
    return pending


def pool_close_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    result = {key: payload[key] for key in (
        "ok", "pool_id", "state", "active_count", "undelivered_count", "error", "cleanup_errors",
    ) if key in payload}
    pending = _pending_jobs(payload.get("jobs", []))
    if pending:
        result["jobs"] = pending
    result["details"] = {"tool": "pi_search_pool_snapshot", "detail": True}
    return result
