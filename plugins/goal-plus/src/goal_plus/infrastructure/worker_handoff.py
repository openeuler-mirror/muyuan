"""Bounded worker handoff and diagnostic observations; no execution authority."""

import json
import subprocess
from pathlib import Path
from typing import Any
from goal_plus.candidate_host import candidate_host_queries


def bounded_diagnostic(value: Any, *, limit: int = 500) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return bounded_diagnostic(value, limit=500)
    if isinstance(value, dict):
        return {
            str(key)[:100]: _bounded_json(item, depth=depth + 1)
            for key, item in list(value.items())[:20]
        }
    if isinstance(value, list):
        return [_bounded_json(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value if len(value) <= 2000 else value[:2000] + "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return bounded_diagnostic(value, limit=500)


def _git_snapshot(workspace: Path) -> dict[str, Any]:
    if not (workspace / ".git").exists():
        return {
            "git_head": None,
            "dirty": False,
            "git_status": [],
            "changed_files": [],
            "diff_stat": None,
        }

    def output(args: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                args,
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        return completed.stdout.rstrip("\n")

    head_output = output(["git", "rev-parse", "--verify", "HEAD"])
    head = head_output.strip() if head_output else None
    status_text = output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"]
    )
    status = [line for line in (status_text or "").splitlines() if line.strip()]

    def ignored(path: str) -> bool:
        parts = Path(path).parts
        return (
            path.startswith(".tmp/")
            or "__pycache__" in parts
            or path.endswith((".pyc", ".pyo"))
        )

    changed_files: list[str] = []
    for line in status:
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path and not ignored(path):
            changed_files.append(path)
    diff_stat = output(["git", "diff", "--stat", "HEAD"]) if head else None
    return {
        "git_head": head,
        "dirty": bool(changed_files),
        "git_status": [
            line
            for line in status
            if not ignored(line[3:] if len(line) > 3 else "")
        ][:100],
        "changed_files": sorted(set(changed_files))[:100],
        "diff_stat": bounded_diagnostic(diff_stat, limit=4000),
    }


def workspace_progress_handoff(
    workspace: Path,
    *,
    root: Path,
    run_id: str,
    candidate_id: str,
    timed_out: bool,
    runner_failed: bool,
    assistant_text: str | None,
) -> dict[str, Any]:
    model_handoff: dict[str, Any] | None = None
    handoff_path = workspace / ".tmp" / "handoff.json"
    try:
        parsed = json.loads(handoff_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            model_handoff = _bounded_json(parsed)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass

    workspace_state = _git_snapshot(workspace)
    verifier_state = candidate_host_queries(root).verifier_snapshot(run_id, candidate_id)
    assistant_summary = bounded_diagnostic(assistant_text, limit=2000)
    summary = (
        model_handoff.get("summary")
        if model_handoff and isinstance(model_handoff.get("summary"), str)
        else assistant_summary
    )
    if not summary:
        changed_files = workspace_state["changed_files"]
        summary = (
            "工作区存在尚未验证的变更：" + ", ".join(changed_files)
            if changed_files
            else "尚未记录 verifier 支持的进展。"
        )
    return {
        "status": "runner_failed" if runner_failed else "timed_out" if timed_out else "completed",
        "summary": summary,
        "model_handoff": model_handoff,
        "assistant_summary": assistant_summary,
        "workspace": workspace_state,
        "verifier": verifier_state,
    }
