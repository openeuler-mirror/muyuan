"""Focused tests for the shared native-process sandbox resolver."""

from pathlib import Path

import pytest

from goal_plus.execution.sandbox import SandboxUnavailable, resolve_sandbox_command
from goal_plus.ports.sandbox import ProcessSandbox


def _sandbox(tmp_path: Path, *, mode: str = "workspace-write", backend: str = "outer") -> ProcessSandbox:
    return ProcessSandbox(
        mode=mode,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        workspace=tmp_path / "workspace",
        writable_roots=(tmp_path / "runtime",),
    )


def test_linux_outer_sandbox_mounts_workspace_and_keeps_json_command_intact(tmp_path: Path) -> None:
    command = resolve_sandbox_command(
        ("pi", "--mode", "rpc"),
        cwd=tmp_path / "workspace",
        sandbox=_sandbox(tmp_path),
        platform="linux",
        which=lambda name: f"/usr/bin/{name}",
    )
    assert command[:2] == ("/usr/bin/bwrap", "--die-with-parent")
    assert command[command.index("--ro-bind"):command.index("--ro-bind") + 3] == ("--ro-bind", "/", "/")
    bind_index = command.index("--bind", command.index("--ro-bind") + 1)
    assert command[bind_index:bind_index + 3] == ("--bind", str(tmp_path / "workspace"), str(tmp_path / "workspace"))
    assert command[-4:] == ("--", "pi", "--mode", "rpc")


def test_macos_outer_sandbox_denies_writes_outside_declared_roots(tmp_path: Path) -> None:
    command = resolve_sandbox_command(
        ("pi", "--mode", "rpc"),
        cwd=tmp_path / "workspace",
        sandbox=_sandbox(tmp_path),
        platform="darwin",
        which=lambda name: "/usr/bin/sandbox-exec",
    )
    assert command[:2] == ("/usr/bin/sandbox-exec", "-p")
    profile = command[2]
    assert "deny file-write*" in profile
    assert str((tmp_path / "workspace").resolve()) in profile
    assert command[-3:] == ("pi", "--mode", "rpc")


def test_native_backend_does_not_wrap_the_agent_command(tmp_path: Path) -> None:
    command = ("codex", "app-server", "--stdio")
    assert resolve_sandbox_command(
        command,
        cwd=tmp_path / "workspace",
        sandbox=_sandbox(tmp_path, backend="native"),
        platform="linux",
        which=lambda name: None,
    ) == command


def test_required_outer_backend_fails_closed_when_unavailable(tmp_path: Path) -> None:
    with pytest.raises(SandboxUnavailable, match="bubblewrap"):
        resolve_sandbox_command(
            ("pi", "--mode", "rpc"),
            cwd=tmp_path / "workspace",
            sandbox=_sandbox(tmp_path),
            platform="linux",
            which=lambda name: None,
        )


def test_bubblewrap_requires_cwd_in_a_declared_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside its declared roots"):
        resolve_sandbox_command(
            ("pi", "--mode", "rpc"),
            cwd=tmp_path / "outside",
            sandbox=_sandbox(tmp_path),
            platform="linux",
            which=lambda name: "/usr/bin/bwrap",
        )
