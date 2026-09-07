from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from goal_plus.inner_git import (
    InnerGitError,
    initialize,
    prepare_attempt,
    settle_attempt,
)


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_inner_git_keeps_private_history_and_restores_best_without_losing_rows(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "child" / "project"
    workspace.mkdir(parents=True)
    product = workspace / "program.py"
    product.write_text("VALUE = 1\n", encoding="utf-8")
    initial = "commit\tscore\tstatus\thypothesis\n"

    initialized = initialize(
        workspace,
        metric_name="score",
        initial_results=initial,
    )
    assert (workspace / ".git").is_dir()
    assert (workspace / "results.tsv").read_text(encoding="utf-8") == initial
    assert git(workspace, "rev-list", "--count", "HEAD") == "2"

    product.write_text("VALUE = 2\n", encoding="utf-8")
    first = prepare_attempt(workspace, dispatch_nonce="dispatch-1")
    assert first["baseline_git_head"] == initialized["baseline_git_head"]
    settle_attempt(
        workspace,
        attempt_git_head=str(first["attempt_git_head"]),
        iteration=1,
        metric_name="score",
        score=2.0,
        status="pass",
        hypothesis="raise value",
        restore_git_head=None,
    )

    product.write_text("VALUE = 0\n", encoding="utf-8")
    second = prepare_attempt(workspace, dispatch_nonce="dispatch-2")
    settled = settle_attempt(
        workspace,
        attempt_git_head=str(second["attempt_git_head"]),
        iteration=2,
        metric_name="score",
        score=0.0,
        status="pass",
        hypothesis="worse value",
        restore_git_head=str(first["attempt_git_head"]),
    )

    assert product.read_text(encoding="utf-8") == "VALUE = 2\n"
    rows = (workspace / "results.tsv").read_text(encoding="utf-8").splitlines()
    assert rows == [
        "commit\tscore\tstatus\thypothesis",
        f"{first['attempt_git_head']}\t2\tpass\traise value",
        f"{second['attempt_git_head']}\t0\tpass\tworse value",
    ]
    assert settled["result_count"] == 2
    assert settled["restored_to_git_head"] == first["attempt_git_head"]
    assert git(workspace, "cat-file", "-t", str(second["attempt_git_head"])) == "commit"
    subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            str(second["attempt_git_head"]),
            "HEAD",
        ],
        cwd=workspace,
        check=True,
    )
    assert "goal-plus restore best before iteration 2" in git(
        workspace,
        "log",
        "--format=%s",
    ).splitlines()
    state = json.loads(
        (workspace / ".git" / "goal-plus" / "state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["attempt_git_heads"] == {
        "1": first["attempt_git_head"],
        "2": second["attempt_git_head"],
    }


def test_inner_git_rejects_worker_results_ledger_mutation(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "program.py").write_text("VALUE = 1\n", encoding="utf-8")
    initialize(
        workspace,
        metric_name="score",
        initial_results="commit\tscore\tstatus\thypothesis\n",
    )
    (workspace / "results.tsv").write_text("worker rewrite\n", encoding="utf-8")

    with pytest.raises(InnerGitError, match="ResultsLedgerMutation"):
        prepare_attempt(workspace, dispatch_nonce="dispatch-1")


def test_inner_git_does_not_commit_nested_private_state(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "program.py").write_text("VALUE = 1\n", encoding="utf-8")
    initialize(
        workspace,
        metric_name="score",
        initial_results="commit\tscore\tstatus\thypothesis\n",
    )
    for relative in (
        "nested/results.tsv",
        "nested/.gp/state.json",
        "nested/.search/state.json",
        "nested/.tmp/scratch.txt",
        "nested/repository/.git/config",
    ):
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private\n", encoding="utf-8")
    (workspace / "program.py").write_text("VALUE = 2\n", encoding="utf-8")

    attempt = prepare_attempt(workspace, dispatch_nonce="dispatch-1")

    assert git(workspace, "show", "--format=", "--name-only", str(attempt["attempt_git_head"])) == (
        "program.py"
    )
