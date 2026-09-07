"""Entry-point contract for the thin ``goal-plus`` CLI dispatcher.

The console script must stay import-light: hosts spawn it once per hook
event, so importing the dispatcher must never pull in the MCP server stack.
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests._runtime_helpers import run_host_hook_inprocess

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


@pytest.mark.parametrize("fail", [False, True])
def test_hook_driver_uses_cwd_and_restores_process_state(tmp_path: Path, monkeypatch, fail: bool) -> None:
    import goal_plus.goal_plus_stop_hook as hook_module

    target = tmp_path / "hook-project"
    target.mkdir()
    (target / ".gp").mkdir()
    saved_cwd = Path.cwd()
    saved_streams = sys.stdin, sys.stdout, sys.stderr
    saved_environment = dict(os.environ)

    def invoke() -> int:
        assert Path.cwd() == target
        assert hook_module._search_root(hook_module._find_session_root()) == target / ".runtime"
        assert json.load(sys.stdin) == {"event": "test"}
        os.environ["HOOK_TEST_MUTATION"] = "temporary"
        print("output")
        print("diagnostic", file=sys.stderr)
        if fail:
            raise RuntimeError("hook test failure")
        return 0

    monkeypatch.setattr(hook_module, "main", invoke)
    kwargs = {"cwd": target, "env": {"GOAL_PLUS_SEARCH_ROOT": ".runtime"}}
    if fail:
        with pytest.raises(RuntimeError, match="hook test failure"):
            run_host_hook_inprocess({"event": "test"}, **kwargs)
    else:
        result = run_host_hook_inprocess({"event": "test"}, **kwargs)
        assert (result.returncode, result.stdout, result.stderr) == (0, "output\n", "diagnostic\n")
    assert Path.cwd() == saved_cwd
    assert (sys.stdin, sys.stdout, sys.stderr) == saved_streams
    assert dict(os.environ) == saved_environment


def _run(args: list[str], *, cwd: Path, input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "goal_plus.cli", *args],
        cwd=cwd,
        input=input,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )


def test_cli_import_does_not_load_mcp_server_stack(tmp_path: Path) -> None:
    code = (
        "import sys; import goal_plus.cli; "
        "print('mcp' in sys.modules, 'fastmcp' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False False"


def test_cli_version_prints_without_heavy_imports(tmp_path: Path) -> None:
    completed = _run(["--version"], cwd=tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "goal-plus 0.1.0"


def test_cli_host_hook_entry_runs_and_exits_clean(tmp_path: Path) -> None:
    completed = _run(
        ["--goal-plus-host-hook"],
        cwd=tmp_path,
        input="{}",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""


def test_cli_rejects_unknown_flags(tmp_path: Path) -> None:
    completed = _run(["--definitely-not-a-flag"], cwd=tmp_path)

    assert completed.returncode != 0
