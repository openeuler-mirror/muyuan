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
            "min_verifier_runs", "allocation_decision_id", "pending_session_refresh_reason",
        ) if key in result["lease"]}
    return payload


def pool_tool_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if "pools" in payload:
        return {**payload, "pools": [pool_tool_summary(pool) for pool in payload["pools"]]}
    result = dict(payload)
    if "events" in payload:
        result["events"] = [
            {**event, "result": worker_result_summary(event.get("result"))}
            for event in payload["events"]
        ]
    if "jobs" in payload:
        jobs = payload["jobs"]
        latest = {job["candidate_id"]: job["job_id"] for job in jobs}
        result["jobs_total"] = len(jobs)
        result["jobs"] = [
            {**job, "result": worker_result_summary(job.get("result"))}
            for job in jobs
            if job["job_id"] == latest[job["candidate_id"]]
            or job["status"] in {"starting", "running"} or not job.get("delivered_at")
        ]
    return result
