"""Shared runtime test helpers.

Extracted from ``tests/test_runtime_unit.py`` so that other test modules can
reuse them without importing the 5000-line runtime test file (which forces
pytest to parse every test there during collection).

These remain plain functions — not pytest fixtures — so callers can pass
arbitrary arguments (``spec_with_strategy(project, strategy_dict)`` etc.).
The thin ``project_dir`` / ``frozen_run`` fixtures in ``tests/conftest.py``
wrap the most common zero-arg patterns for new tests that prefer injection.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import GoalPlusHostCommandInvocation, GoalPlusRecord, SearchSpec
from goal_plus.runtime import FileSearchRuntime


@dataclass
class HookResult:
    """Duck-typed stand-in for ``subprocess.CompletedProcess[str]``.

    Tests keep asserting on ``returncode`` / ``stdout`` / ``stderr`` while the
    hook runs in-process, avoiding a ~1.4s interpreter+import cold start per
    invocation.
    """

    returncode: int
    stdout: str
    stderr: str


def run_host_hook_inprocess(
    hook_input: dict | None,
    *,
    cwd: Path,
    env: dict[str, str],
) -> HookResult:
    """Run ``goal_plus.goal_plus_stop_hook.main()`` inside this process.

    cwd, stdin/stdout/stderr and the environment are swapped for the call.
    This does not simulate process identity or import isolation. Hooks
    normally terminate via ``return``; a stray ``SystemExit`` is treated as
    the CLI exit code.
    """
    import goal_plus.goal_plus_stop_hook as hook_module

    saved = sys.stdin, sys.stdout, sys.stderr
    saved_cwd = Path.cwd()
    saved_environ = os.environ.copy()
    try:
        os.chdir(cwd)
        sys.stdin = io.StringIO(json.dumps(hook_input or {}))
        sys.stdout = out = io.StringIO()
        sys.stderr = err = io.StringIO()
        os.environ.clear()
        os.environ.update(env)
        try:
            code = hook_module.main()
        except SystemExit as exc:  # pragma: no cover - defensive
            code = exc.code if isinstance(exc.code, int) else 0
        return HookResult(returncode=int(code or 0), stdout=out.getvalue(), stderr=err.getvalue())
    finally:
        sys.stdin, sys.stdout, sys.stderr = saved
        os.environ.clear()
        os.environ.update(saved_environ)
        os.chdir(saved_cwd)


def start_host_verified_goal(
    root: Path,
    source_path: Path,
    *,
    session_id: str = "session-test",
    invocation_id: str = "codex:test:start",
    native_host: str = "codex",
) -> GoalPlusRecord:
    command = "$goal-plus" if native_host == "codex" else "/goal-plus"
    raw_input = f"{command} Test"
    runtime = FileGoalPlusRuntime(root)
    record = runtime.start_from_host_command(
        "Test Goal Plus search",
        source_path=str(source_path),
        invocation=GoalPlusHostCommandInvocation(
            host=native_host,
            transport="codex_user_prompt_submit" if native_host == "codex" else "pi_extension_command",
            command=command,
            action="start",
            session_id=session_id,
            native_entry_id="turn-test",
            invocation_id=invocation_id,
            raw_input_sha256=hashlib.sha256(raw_input.encode()).hexdigest(),
            invoked_at="2026-08-28T00:00:00Z",
        ),
    )
    return runtime.activate_session(
        record.goal_plus_id,
        {
            "host": "codex" if native_host == "codex" else "pi-rpc", "session_id": session_id,
            "attached_at": record.created_at, "last_seen_at": record.updated_at,
        },
    )


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "initial_program.py").write_text("VALUE = 0\n", encoding="utf-8")
    (project / "evaluator.py").write_text(
        "import json\n"
        "def evaluate(_path):\n"
        "    return {'combined_score': 0.0}\n"
        "if __name__ == '__main__':\n"
        "    print(json.dumps(evaluate('initial_program.py')))\n",
        encoding="utf-8",
    )
    (project / "config.yaml").write_text("name: toy\n", encoding="utf-8")
    return project


def spec_for(project: Path, *, max_parallel: int = 4, direction: str = "maximize") -> SearchSpec:
    return SearchSpec.model_validate(
        {
            "objective": "test runtime",
            "metric_name": "combined_score",
            "metric_direction": direction,
            "source_path": str(project),
            "edit_surface": {
                "allow": ["initial_program.py"],
                "deny": ["evaluator.py", "config.yaml"],
            },
            "budget": {
                "max_parallel": max_parallel,
            },
            "process_verifiers": [
                {
                    "name": "score",
                    "role": "ranking_signal",
                    "command": ["python", "evaluator.py"],
                    "timeout_seconds": 30,
                }
            ],
            "strategy": {"name": "random"},
            "workspace": {"backend": "copy"},
        }
    )


def spec_with_strategy(
    project: Path,
    strategy: dict,
    *,
    max_parallel: int = 4,
) -> SearchSpec:
    data = spec_for(project, max_parallel=max_parallel).model_dump(mode="json")
    data["strategy"] = strategy
    return SearchSpec.model_validate(data)


def create_candidate(
    runtime: FileSearchRuntime,
    project: Path,
    *,
    direction: str = "maximize",
) -> tuple[str, str, Path]:
    frozen = runtime.freeze_spec(spec_for(project, direction=direction), [project / "evaluator.py"], native_host="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    return run_id, task.candidate_id, task.workspace


def git_commit_all(workspace: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            message,
        ],
        cwd=workspace,
        check=True,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
    ).strip()


def process_is_running(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        fields = stat_path.read_text(encoding="utf-8").split()
        if len(fields) > 2 and fields[2] == "Z":
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
