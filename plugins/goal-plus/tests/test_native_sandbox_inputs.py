"""Pi/Codex native launch declarations use their correct sandbox backend."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus.infrastructure.native_session_inputs import native_candidate_input
from goal_plus.infrastructure.state.resource_locks import VERIFIER_RESOURCE_LOCK_DIR_ENV


@pytest.mark.parametrize(
    ("agent_harness", "backend"),
    [("pi", "outer"), ("codex", "native")],
)
def test_direct_candidate_declares_host_specific_sandbox(tmp_path: Path, agent_harness: str, backend: str, monkeypatch) -> None:
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
    lock_root = tmp_path / "host-locks" / "shared"
    monkeypatch.setenv(VERIFIER_RESOURCE_LOCK_DIR_ENV, str(lock_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SimpleNamespace(
        agent_harness=agent_harness,
        runtime_provider="direct",
        agent_session_id="agent-1",
        workspace=workspace,
        launch=(
            {"model_pattern": "provider/model", "thinking_level": "low"}
            if agent_harness == "pi"
            else {"model": "model", "reasoning_effort": "low"}
        ),
        session_handle=SimpleNamespace(external_id=None, metadata={}),
    )
    scope = {
        "agent_harness": agent_harness,
        "native_agent_config": (
            {"model_pattern": "provider/model", "thinking_level": "low"}
            if agent_harness == "pi"
            else {"model": "model", "reasoning_effort": "low"}
        ),
    }
    result = native_candidate_input(
        tmp_path / ".gp", session, scope, process_environment={},
    )
    assert result.process.environment["GOAL_PLUS_DEBUG"] == "1"
    assert result.process.sandbox is not None
    assert result.process.sandbox.backend == backend
    assert result.process.sandbox.mode == "workspace-write"
    assert result.process.sandbox.workspace == workspace
    assert result.process.environment[VERIFIER_RESOURCE_LOCK_DIR_ENV] == str(lock_root)
    assert lock_root.is_dir()
    assert result.process.sandbox.writable_roots == (tmp_path / ".gp", tmp_path / ".gp/host-sessions" / agent_harness, lock_root)


def test_pi_fork_resume_attaches_the_provider_session_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(VERIFIER_RESOURCE_LOCK_DIR_ENV, str(tmp_path / "host-locks"))
    root = tmp_path / ".gp"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_dir = root / "host-sessions" / "pi" / "sessions" / "encoded-workspace"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "2026-01-01T00-00-00-000Z_agent-1.jsonl"
    session_file.write_text(
        json.dumps({
            "type": "session",
            "version": 3,
            "id": "agent-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "cwd": workspace.as_posix(),
        }) + "\n"
    )
    session = SimpleNamespace(
        agent_harness="pi",
        runtime_provider="direct",
        agent_session_id="agent-1",
        workspace=workspace,
        candidate_origin=SimpleNamespace(),
        launch={"model_pattern": "provider/model", "thinking_level": "low"},
        session_handle=SimpleNamespace(external_id="agent-1", metadata={}),
    )
    scope = {
        "agent_harness": "pi",
        "native_agent_config": {"model_pattern": "provider/model", "thinking_level": "low"},
    }
    result = native_candidate_input(root, session, scope, process_environment={}, resume=True)
    command = result.process.command
    assert command[command.index("--session") + 1] == str(session_file.resolve())
