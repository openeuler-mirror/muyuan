"""Sandboxed Pi retains model access without changing its host config home."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus.infrastructure.native_session_inputs import native_candidate_input


@pytest.mark.parametrize("configured_home", [False, True])
def test_pi_candidate_copies_host_config_and_preserves_existing_session_config(
    tmp_path, monkeypatch, configured_home,
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    source = tmp_path / "pi-config" if configured_home else home / ".pi/agent"
    if configured_home:
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(source))
    else:
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    source.mkdir(parents=True)
    auth = '{"test-provider":{"type":"api_key","key":"fixture-only"}}'
    (source / "auth.json").write_text(auth)
    (source / "models.json").write_text('{"providers":{}}')
    (source / "settings.json").write_text('{"defaultModel":"test-model"}')
    (source / "unrelated.txt").write_text("do not copy")
    root = tmp_path / ".gp"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SimpleNamespace(
        agent_harness="pi", runtime_provider="direct", agent_session_id="agent-1",
        workspace=workspace, launch={},
        session_handle=SimpleNamespace(external_id=None, metadata={}),
    )
    scope = {"agent_harness": "pi", "native_agent_config": {"model_pattern": "test-provider/test-model"}}
    result = native_candidate_input(root, session, scope, process_environment={})
    target = Path(result.process.environment["PI_CODING_AGENT_DIR"])
    assert target != source
    command = result.process.command
    session_dir = Path(command[command.index("--session-dir") + 1])
    # Pi migrates loose JSONL files in its config home on the next startup.
    assert target != session_dir
    assert (target / "auth.json").read_text() == auth
    assert (target / "auth.json").stat().st_mode & 0o777 == 0o600
    assert (target / "models.json").read_bytes() == (source / "models.json").read_bytes()
    assert not (target / "unrelated.txt").exists()
    (target / "settings.json").write_text('{"defaultModel":"session-model"}')
    native_candidate_input(root, session, scope, process_environment={})
    assert json.loads((target / "settings.json").read_text())["defaultModel"] == "session-model"
    assert json.loads((source / "settings.json").read_text())["defaultModel"] == "test-model"
