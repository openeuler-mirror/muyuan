"""Baseline admission must protect runtime state before a provider captures Root."""

from unittest.mock import Mock

import pytest

from goal_plus.infrastructure.artifacts.thinkthread_baseline import ThinkThreadBaseline


@pytest.mark.parametrize("layout", ["root", "nested", "symlink"])
def test_runtime_inside_root_is_rejected_before_provider_access(tmp_path, monkeypatch, layout):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    runtime = workspace if layout == "root" else workspace / "private-runtime"
    runtime.mkdir(exist_ok=True)
    secret = runtime / "goal.json"
    secret.write_text("private Goal context")
    if layout == "symlink":
        alias = tmp_path / "runtime-alias"
        alias.symlink_to(runtime, target_is_directory=True)
        runtime = alias
    sdk = Mock()

    with pytest.raises(ValueError, match="runtime root must be outside"):
        ThinkThreadBaseline(runtime, sdk, ".")

    assert sdk.mock_calls == []
    assert secret.read_text() == "private Goal context"


def test_session_directory_outside_root_is_accepted(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    sdk = Mock()
    monkeypatch.setattr(
        "goal_plus.infrastructure.artifacts.thinkthread_baseline.validate_thinkthread_root",
        lambda client, **_: {"thinkthreadId": "tt-root"},
    )
    baseline = ThinkThreadBaseline(tmp_path / "session" / "goal-plus", sdk, ".")
    assert baseline.provider_handle["root_thinkthread_id"] == "tt-root"
    assert sdk.mock_calls == []
