"""The optional local push gate must test HEAD and propagate pytest failures."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(os.name != "posix", reason="POSIX Git hook")
@pytest.mark.parametrize("case", ["pass", "failed_tests", "dirty", "other_commit", "delete"])
def test_platform_push_gate(tmp_path, case):
    repository = tmp_path / "repo"
    plugin = repository / "plugins/goal-plus"
    plugin.mkdir(parents=True)
    tracked = plugin / "tracked.txt"
    tracked.write_text("original")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture",
    ], cwd=repository, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    hook = tmp_path / "pre-push"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/git-hooks/pre-push", hook)
    python = tmp_path / "python"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GP_TEST_HOOK_LOG"\nexit "$GP_TEST_HOOK_EXIT"\n')
    python.chmod(0o755)
    log = tmp_path / "arguments"
    environment = {**os.environ, "GOAL_PLUS_TEST_PYTHON": str(python),
                   "GP_TEST_HOOK_LOG": str(log), "GP_TEST_HOOK_EXIT": "17" if case == "failed_tests" else "0"}
    if case == "dirty":
        tracked.write_text("uncommitted fix")
    local = "0" * 40 if case == "delete" else ("f" * 40 if case == "other_commit" else head)
    result = subprocess.run(
        ["sh", str(hook)], input=f"refs/heads/test {local} refs/heads/test {'0' * 40}\n",
        cwd=repository, env=environment, text=True, capture_output=True, timeout=10,
    )
    if case in ("pass", "failed_tests"):
        assert result.returncode == (17 if case == "failed_tests" else 0)
        assert "tests/test_process_identity_darwin.py" in log.read_text().splitlines()
        assert "addopts=" in log.read_text().splitlines()
    else:
        assert result.returncode == (0 if case == "delete" else 1)
        assert not log.exists()
