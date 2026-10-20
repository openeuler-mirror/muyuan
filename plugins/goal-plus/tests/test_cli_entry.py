"""Entry-point contract for the thin ``goal-plus`` CLI dispatcher.

The console script must stay import-light: hosts spawn it once per hook
event, so importing the dispatcher must never pull in the MCP server stack.
"""

from __future__ import annotations

import os
import asyncio
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


def test_cli_installation_info_reports_invoked_runtime_without_creating_state(tmp_path: Path) -> None:
    completed = _run(["--installation-info"], cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    identity = json.loads(completed.stdout)
    assert identity["python"] == sys.executable
    assert identity["module"] == str(SRC / "goal_plus")
    assert "build" in identity
    assert not (tmp_path / ".gp").exists()


@pytest.mark.parametrize("host", ["codex", "pi"])
def test_cli_runtime_info_prints_without_mcp_or_state(tmp_path, host):
    completed = _run(["--runtime-info", host, "--json"], cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    info = json.loads(completed.stdout)
    assert info["host"]["agent_harness"] == host
    assert info["installation"]["python"] == sys.executable
    assert info["sandbox"]["execution_verified"] is False
    assert not (tmp_path / ".gp").exists()
    code = ("import sys; from goal_plus.cli import main; "
            f"sys.argv=['goal-plus', '--runtime-info', '{host}']; main(); "
            "assert 'fastmcp' not in sys.modules and 'goal_plus.server' not in sys.modules")
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, text=True, capture_output=True,
                            env={**os.environ, "PYTHONPATH": str(SRC)})
    assert result.returncode == 0, result.stderr
    assert info["installation"]["python"] in result.stdout


@pytest.mark.parametrize("host", ["codex", "pi"])
def test_runtime_provider_selection_is_explicit_and_thinkthread_root_is_unknown(tmp_path, monkeypatch, host):
    monkeypatch.setenv("GOAL_PLUS_THINKTHREAD_ROLE", "main")
    monkeypatch.setenv("GOAL_PLUS_AGENT_POSIX_SDK_ENTRY", "/unused/sdk")
    for provider in ("direct", "thinkthread"):
        result = _run(["--runtime-info", host, "--runtime-provider", provider, "--json"], cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        info = json.loads(result.stdout)
        assert info["host"] == {"agent_harness": host, "runtime_provider": provider}
        assert info["runtime"]["root"] == (None if provider == "thinkthread" else str(tmp_path / ".gp"))
        assert info["sandbox"]["execution_verified"] is False
        if provider == "thinkthread":
            assert info["sandbox"]["worker_backend"] == "thinkthread"
            assert info["sandbox"]["backend_executable"] is None
    assert not (tmp_path / ".gp").exists()


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


@pytest.mark.parametrize("role", ["main", "candidate", "ordinary"])
def test_cli_mcp_uses_host_role_and_shared_overview(tmp_path: Path, role: str) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    root = tmp_path / "runtime"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GOAL_PLUS_")}
    environment["PYTHONPATH"] = str(SRC)
    if role == "candidate":
        environment.update({
            "GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID": "bound-candidate",
            "GOAL_PLUS_SEARCH_ROOT": str(root),
        })
    elif role == "ordinary":
        environment.update({"GOAL_PLUS_CODEX_ORDINARY_ROLE": "ordinary", "GOAL_PLUS_SEARCH_ROOT": str(root)})

    async def exercise():
        transport = StdioTransport(
            command=sys.executable, args=["-m", "goal_plus.cli", "--root", str(tmp_path / "cli-root")],
            env=environment, cwd=str(tmp_path),
        )
        async with Client(transport, timeout=20) as client:
            names = {tool.name for tool in await client.list_tools()}
            overview = client.initialize_result.instructions if client.initialize_result is not None else client.instructions
            if role == "main":
                expected = (ROOT / "assets/goal-plus-instructions.txt").read_text().strip()
                assert overview == expected
                summary, supplement = overview.split("\n", 1)
                # Codex ToolsState keeps only the first line, up to 250 characters.
                assert 0 < len(summary) <= 250
                assert supplement.strip() and len(overview) <= 500
                # Plugin namespace descriptions are capped by UTF-8 bytes.
                assert len(overview.encode("utf-8")) <= 1000
                assert "goal_plus_session_run" in names
                assert "goal_plus_search_select" in names
            elif role == "ordinary":
                assert not overview
                assert not names
            else:
                assert not overview
                assert "goal_plus_session_run" not in names
                assert "goal_plus_search_select" not in names
                result = await client.call_tool("goal_plus_search_get_agent_context", {
                    "agent_session_id": "foreign-candidate",
                }, raise_on_error=False)
                assert result.is_error and "Unexpected keyword argument" in result.content[0].text

    asyncio.run(exercise())
    if role == "candidate":
        assert not (tmp_path / "cli-root").exists()


def test_codex_candidate_factory_uses_host_declared_root(tmp_path, monkeypatch):
    from goal_plus import server

    root = tmp_path / "host-root"
    monkeypatch.setenv("GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID", "candidate")
    monkeypatch.setenv("GOAL_PLUS_SEARCH_ROOT", str(root))
    monkeypatch.setattr(server, "create_host_mcp", lambda root_dir, **kwargs: (root_dir, kwargs))
    assert server.create_codex_mcp(tmp_path / "other-root") == (str(root), {
        "agent_harness": "codex", "caller_role": "candidate", "caller_agent_session_id": "candidate",
        "caller_final_check": None,
    })


@pytest.mark.parametrize("root", [None, "relative/runtime"])
def test_candidate_cli_requires_host_declared_absolute_root(tmp_path, monkeypatch, root):
    monkeypatch.setenv("GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID", "candidate")
    if root is None:
        monkeypatch.delenv("GOAL_PLUS_SEARCH_ROOT", raising=False)
    else:
        monkeypatch.setenv("GOAL_PLUS_SEARCH_ROOT", root)
    completed = _run(["--root", str(tmp_path / "model-root")], cwd=tmp_path)
    assert completed.returncode != 0
    assert "host-declared absolute runtime root" in completed.stderr
    assert not (tmp_path / "model-root").exists()
