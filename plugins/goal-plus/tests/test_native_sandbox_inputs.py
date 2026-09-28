"""Pi/Codex native launch declarations use their correct sandbox backend."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus.infrastructure.native_session_inputs import native_candidate_input


@pytest.mark.parametrize(
    ("agent_harness", "backend"),
    [("pi", "outer"), ("codex", "native")],
)
def test_direct_candidate_declares_host_specific_sandbox(tmp_path: Path, agent_harness: str, backend: str, monkeypatch) -> None:
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
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
    assert result.process.sandbox.writable_roots
