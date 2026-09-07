from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_installer() -> ModuleType:
    path = ROOT / "scripts" / "install.py"
    spec = importlib.util.spec_from_file_location("goal_plus_install", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installer() -> ModuleType:
    return _load_installer()


def _write_pi_settings(agent_dir: Path, packages: list[object]) -> None:
    agent_dir.mkdir(parents=True)
    (agent_dir / "settings.json").write_text(
        json.dumps({"packages": packages}),
        encoding="utf-8",
    )


def _thinkthread_pi_profile() -> dict[str, object]:
    return {
        "schemaVersion": 4,
        "agent": {
            "name": "pi",
            "executable": "pi",
            "args": [],
            "configDirEnv": "PI_CODING_AGENT_DIR",
            "configDirDefault": "/home/test/.pi/agent",
            "configDirAccess": "read_write",
            "extension": {
                "schemaVersion": 1,
                "name": "pi",
                "loader": {
                    "kind": "process",
                    "command": ["thinkthread-extension-pi"],
                    "protocolVersion": 5,
                },
            },
        },
        "environment": {
            "fs": ".",
            "rootFsMode": "direct",
            "childFsMode": "copy_on_write",
            "read": [],
            "write": [],
            "network": {"mode": "all"},
        },
        "delegation": {
            "capabilities": {
                "allow": ["thinkthread.message", "thinkthread.fs.self"]
            }
        },
    }


def test_install_entrypoint_is_executable() -> None:
    entrypoint = ROOT / "install.sh"

    assert entrypoint.is_file()
    assert entrypoint.stat().st_mode & 0o111


def test_pi_dev_assets_scope_only_the_launcher(
    installer: ModuleType,
    tmp_path: Path,
) -> None:
    dev_dir = tmp_path / "dev"
    set_env = installer._render_set_env(dev_dir)
    launcher = installer._render_pi_launcher(
        dev_dir=dev_dir,
        pi_executable="/opt/pi/bin/pi",
    )
    launcher_path = tmp_path / "pi-goal-plus"
    launcher_path.write_text(launcher, encoding="utf-8")

    assert "GOAL_PLUS_PI_DEV_ROOT" in set_env
    assert str(dev_dir / "bin") in set_env
    assert str(dev_dir / "venv" / "bin") not in set_env
    assert "CODEX_HOME" not in set_env
    assert "PI_CODING_AGENT_DIR" not in set_env
    assert "--extension" in launcher
    assert "--no-extensions" in launcher
    assert "--no-prompt-templates" in launcher
    assert launcher.index("--no-extensions") < launcher.index("--extension")
    assert str(ROOT / ".pi" / "extensions" / "goal-plus.ts") in launcher
    assert "--skill" in launcher
    assert str(ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md") in launcher
    assert 'export PATH="$goal_plus_venv/bin:$PATH"' in launcher
    assert "--check-pi-dev-runtime" in launcher
    subprocess.run(["bash", "-n", str(launcher_path)], check=True)


def test_pi_dev_usage_output_explains_same_shell_and_target_workspace(
    installer: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dev_dir = tmp_path / "dev"

    installer._print_pi_dev_usage(dev_dir, dry_run=False)

    output = capsys.readouterr().out
    assert "后续使用方式（请在当前 shell 执行）：" in output
    assert f"source {(dev_dir / 'set_env.sh').resolve()}" in output
    assert "cd <target-workspace>" in output
    assert "pi-goal-plus" in output
    assert "打开新的 shell 后，需要重新执行上面的 source 命令。" in output


def test_pi_dev_accepts_same_checkout_user_registration(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _write_pi_settings(agent_dir, [str(ROOT)])
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    installer._preflight_pi_dev()


def test_pi_registration_resolves_relative_source_from_symlinked_agent_path(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_parent = tmp_path / "real" / "nested"
    real_parent.mkdir(parents=True)
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    agent_dir = alias_parent / "pi-agent"
    relative_source = os.path.relpath(ROOT.resolve(), start=agent_dir)
    _write_pi_settings(agent_dir, [relative_source])
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    entries = installer._pi_goal_plus_entries()

    assert len(entries) == 1
    assert entries[0]["resolved_path"] == ROOT.resolve()


def test_pi_dev_ignores_broken_loose_resource_symlinks(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _write_pi_settings(agent_dir, [str(ROOT)])
    extension = agent_dir / "extensions" / "goal-plus.ts"
    skill = agent_dir / "skills" / "goal-plus"
    extension.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True)
    extension.symlink_to(tmp_path / "missing" / "goal-plus.ts")
    skill.symlink_to(tmp_path / "missing" / "goal-plus")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    installer._preflight_pi_dev()


def test_env_show_lists_registered_and_current_environments(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / "state"
    source = tmp_path / "source"
    source.mkdir()
    registered = tmp_path / "registered-dev"
    current = tmp_path / "current-dev"
    for dev_dir, commit in ((registered, "abc123"), (current, "def456")):
        python = dev_dir / "venv" / "bin" / "python"
        launcher = dev_dir / "bin" / "pi-goal-plus"
        python.parent.mkdir(parents=True)
        launcher.parent.mkdir(parents=True)
        python.write_text("python", encoding="utf-8")
        launcher.write_text("launcher", encoding="utf-8")
        (dev_dir / "installation.json").write_text(
            json.dumps(
                {
                    "schema": installer.DEV_SCHEMA,
                    "status": "ready",
                    "source_path": str(source),
                    "venv_python": str(python),
                    "launcher": str(launcher),
                    "source_commit": commit,
                    "source_dirty": False,
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setenv("GOAL_PLUS_INSTALL_STATE_DIR", str(state_dir))
    monkeypatch.setattr(installer, "_git_worktree_pi_dev_dirs", lambda: {current})
    monkeypatch.setattr(installer, "_git_head", lambda _path: "fedcba")
    monkeypatch.setattr(installer, "_git_dirty", lambda _path: True)
    monkeypatch.setattr(installer, "_python_version", lambda _path: "Python 3.12.0")
    monkeypatch.setattr(installer, "_allocated_size_bytes", lambda _path: 1024)
    installer._register_pi_dev_environment(registered)

    installer._show_pi_dev_environments(current_dev_dir=current)

    output = capsys.readouterr().out
    assert "Pi development environments: 2" in output
    assert "[ready] 1.0 KiB" in output
    assert str(registered) in output
    assert str(current) in output
    assert "prepared=abc123 (clean), current=fedcba (dirty)" in output
    assert "Total: 2 existing, 0 missing, 2.0 KiB allocated" in output


def test_env_show_is_read_only_mode(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown: list[Path] = []
    monkeypatch.setattr(
        installer,
        "_show_pi_dev_environments",
        lambda *, current_dev_dir: shown.append(current_dev_dir),
    )

    assert installer.main(["--env-show", "--pi-dev-dir", str(tmp_path)]) == 0
    assert shown == [tmp_path]


def test_pi_dev_detects_stale_local_goal_plus_path(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _write_pi_settings(agent_dir, ["../missing-checkout/goal-plus"])
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    with pytest.raises(installer.InstallError, match="missing-checkout/goal-plus"):
        installer._preflight_pi_dev()


def test_pi_dev_rejects_project_local_goal_plus_assets(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _write_pi_settings(agent_dir, [])
    project = tmp_path / "project"
    extension = project / ".pi" / "extensions" / "goal-plus.ts"
    extension.parent.mkdir(parents=True)
    extension.write_text("// duplicate\n", encoding="utf-8")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    with pytest.raises(installer.InstallError, match=r"\[project\].*goal-plus\.ts"):
        installer._preflight_pi_dev(cwd=project)


def test_pi_normal_accepts_same_checkout_registration(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _write_pi_settings(agent_dir, [str(ROOT)])
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    assert installer._preflight_pi_normal() is True


def test_pi_normal_accepts_same_checkout_loose_resource(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _write_pi_settings(agent_dir, [])
    skill = agent_dir / "skills" / "goal-plus"
    skill.parent.mkdir(parents=True)
    skill.symlink_to(ROOT / ".pi" / "skills" / "goal-plus", target_is_directory=True)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    assert installer._preflight_pi_normal() is False


def test_pi_normal_rejects_loose_goal_plus_assets(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _write_pi_settings(agent_dir, [])
    loose_extension = agent_dir / "extensions" / "goal-plus.ts"
    loose_extension.parent.mkdir(parents=True)
    loose_extension.write_text("// old install\n", encoding="utf-8")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    with pytest.raises(installer.InstallError, match="conflicting Goal Plus"):
        installer._preflight_pi_normal()


def test_pi_normal_prints_conflicting_checkout_uninstall_command(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    conflicting_checkout = tmp_path / "old checkout" / "goal-plus"
    conflicting_checkout.mkdir(parents=True)
    (conflicting_checkout / "uninstall.sh").write_text(
        "#!/usr/bin/env bash\n", encoding="utf-8"
    )
    _write_pi_settings(agent_dir, [str(conflicting_checkout)])
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    with pytest.raises(installer.InstallError) as exc_info:
        installer._preflight_pi_normal()

    assert (
        f"(cd '{conflicting_checkout}' && ./uninstall.sh --pi)"
        in str(exc_info.value)
    )
    assert "Then rerun this installer." in str(exc_info.value)


def test_codex_preflight_rejects_development_plugin(
    installer: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        installer,
        "_codex_inventory",
        lambda _codex: [
            {
                "pluginId": "goal-plus@goal-plus-local",
                "name": "goal-plus",
                "source": {"path": "/tmp/goal-plus-local/plugins/goal-plus"},
            }
        ],
    )
    monkeypatch.setattr(installer, "_codex_marketplaces", lambda _codex: {})

    with pytest.raises(
        installer.InstallError,
        match="codex plugin remove goal-plus@goal-plus-local",
    ):
        installer._preflight_codex("codex")


def test_codex_install_refreshes_existing_plugin(
    installer: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        installer,
        "_run",
        lambda argv, **_kwargs: commands.append(argv),
    )

    installer._install_codex(
        codex="codex",
        marketplace_exists=True,
        dry_run=False,
    )

    assert commands == [["codex", "plugin", "add", "goal-plus@goal-plus"]]


def test_normal_runtime_rejects_commands_from_another_environment(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_bin = tmp_path / "selected" / "bin"
    other_bin = tmp_path / "other" / "bin"
    selected_bin.mkdir(parents=True)
    other_bin.mkdir(parents=True)
    monkeypatch.setenv("PATH", str(selected_bin))
    monkeypatch.setattr(
        installer.sysconfig, "get_path", lambda _name: str(selected_bin)
    )
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: str(other_bin / name) if name == "goal-plus" else None,
    )

    with pytest.raises(installer.InstallError, match="another Python environment"):
        installer._preflight_normal_runtime()


def test_normal_runtime_accepts_selected_scripts_directory(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_bin = tmp_path / "selected" / "bin"
    selected_bin.mkdir(parents=True)
    monkeypatch.setenv("PATH", str(selected_bin))
    monkeypatch.setattr(
        installer.sysconfig, "get_path", lambda _name: str(selected_bin)
    )
    monkeypatch.setattr(installer.shutil, "which", lambda _name: None)

    assert installer._preflight_normal_runtime() == selected_bin.resolve()


def test_pi_dev_dry_run_does_not_create_managed_directory(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _write_pi_settings(agent_dir, [])
    dev_dir = tmp_path / "pi-dev"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    monkeypatch.setattr(
        installer, "_require_executable", lambda _name: "/opt/pi/bin/pi"
    )
    monkeypatch.setattr(
        installer,
        "_git_source_state",
        lambda: {"source_commit": "abc123", "source_dirty": False},
    )

    assert installer.main(["--pi-dev", "--pi-dev-dir", str(dev_dir), "--dry-run"]) == 0
    assert not dev_dir.exists()


def test_pi_dev_install_prefers_binary_dependencies(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        installer,
        "_run",
        lambda argv, **_kwargs: commands.append(argv),
    )
    monkeypatch.setattr(
        installer,
        "_git_source_state",
        lambda: {"source_commit": "abc123", "source_dirty": False},
    )

    installer._prepare_pi_dev(
        dev_dir=tmp_path / "pi-dev",
        pi_executable="/opt/pi/bin/pi",
        dry_run=True,
    )

    pip_command = next(command for command in commands if "pip" in command)
    assert "--prefer-binary" in pip_command
    assert f"{ROOT}[dev]" in pip_command


def test_pi_install_modes_are_mutually_exclusive(installer: ModuleType) -> None:
    with pytest.raises(SystemExit) as raised:
        installer.main(["--pi", "--pi-dev"])

    assert raised.value.code == 2


def test_thinkthread_install_requires_normal_pi(installer: ModuleType) -> None:
    with pytest.raises(SystemExit) as raised:
        installer.main(["--thinkthread"])

    assert raised.value.code == 2


def test_thinkthread_profile_explicitly_binds_goal_plus_runtime(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_prefix = tmp_path / "venv"
    python = python_prefix / "bin" / "python"
    node_prefix = tmp_path / "node"
    node = node_prefix / "bin" / "node"
    sdk_entry = tmp_path / "sdk" / "node_modules" / "@thinkthread" / "agent-posix" / "dist" / "index.js"
    bridge = tmp_path / "bridge.mjs"
    monkeypatch.setattr(installer.sys, "prefix", str(python_prefix))
    monkeypatch.setattr(installer.sys, "executable", str(python))

    profile = installer._build_thinkthread_profile(
        _thinkthread_pi_profile(),
        node_executable=str(node),
        sdk_entry=sdk_entry,
        bridge_path=bridge,
    )
    rendered = installer._render_thinkthread_profile(profile)
    parsed = installer._parse_thinkthread_profile(rendered, source="test")

    arguments = parsed["agent"]["args"]
    assert arguments == [
        "--extension",
        str(ROOT / ".pi" / "extensions" / "goal-plus.ts"),
        "--skill",
        str(ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md"),
    ]
    assert str(ROOT) in parsed["environment"]["read"]
    assert str(python_prefix) in parsed["environment"]["read"]
    assert str(node_prefix) in parsed["environment"]["read"]
    for role in ("root", "child"):
        role_environment = parsed["environment"][role]
        assert role_environment["GOAL_PLUS_PYTHON"] == str(python)
        assert role_environment["GOAL_PLUS_SOURCE_PATH"] == str(ROOT)
        assert role_environment["GOAL_PLUS_ROOT"] == (
            "${PI_CODING_AGENT_SESSION_DIR}/goal-plus"
        )
        assert role_environment["GOAL_PLUS_PI_ROLE"] == (
            "main" if role == "root" else "worker"
        )
        assert role_environment["GOAL_PLUS_AGENT_POSIX_SDK_ENTRY"] == str(
            sdk_entry.resolve()
        )
        assert role_environment["GOAL_PLUS_NODE"] == str(node.resolve())
    assert parsed["environment"]["root"]["GOAL_PLUS_AGENT_POSIX_BRIDGE"] == str(
        bridge.resolve()
    )
    assert "GOAL_PLUS_AGENT_POSIX_BRIDGE" not in parsed["environment"]["child"]


def test_thinkthread_profile_exposes_python_symlink_chain(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_store = tmp_path / "python"
    actual_prefix = python_store / "cpython-3.11.15"
    actual_python = actual_prefix / "bin" / "python3.11"
    actual_python.parent.mkdir(parents=True)
    actual_python.touch()
    alias_prefix = python_store / "cpython-3.11"
    alias_prefix.symlink_to(actual_prefix, target_is_directory=True)
    python_prefix = tmp_path / "venv"
    python = python_prefix / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(alias_prefix / "bin" / "python3.11")
    node = tmp_path / "node" / "bin" / "node"
    monkeypatch.setattr(installer.sys, "prefix", str(python_prefix))
    monkeypatch.setattr(installer.sys, "executable", str(python))

    profile = installer._build_thinkthread_profile(
        _thinkthread_pi_profile(),
        node_executable=str(node),
    )

    readable = profile["environment"]["read"]
    assert str(python_prefix) in readable
    assert str(python_store) in readable
    assert str(actual_prefix) not in readable


def test_thinkthread_profile_preflight_is_idempotent(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = tmp_path / "goal-plus-pi.toml"
    base = installer._render_thinkthread_profile(_thinkthread_pi_profile())

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "thinkthread 0.1.0\n" if "--version" in argv else base
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(installer, "_run", run)
    first = installer._preflight_thinkthread_profile(
        "/opt/bin/tt",
        profile_path=profile_path,
    )
    assert first.installed is False
    profile_path.write_text(first.content, encoding="utf-8")

    second = installer._preflight_thinkthread_profile(
        "/opt/bin/tt",
        profile_path=profile_path,
    )
    assert second.installed is True


def test_thinkthread_profile_preflight_refuses_unmanaged_file(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = tmp_path / "goal-plus-pi.toml"
    profile_path.write_text("schemaVersion = 4\n", encoding="utf-8")
    base = installer._render_thinkthread_profile(_thinkthread_pi_profile())

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "thinkthread 0.1.0\n" if "--version" in argv else base
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(installer, "_run", run)

    with pytest.raises(installer.InstallError, match="not managed"):
        installer._preflight_thinkthread_profile(
            "/opt/bin/tt",
            profile_path=profile_path,
        )


def test_thinkthread_profile_preflight_updates_profile_owned_by_checkout(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = tmp_path / "goal-plus-pi.toml"
    profile_path.write_text(
        f"{installer.THINKTHREAD_PROFILE_MARKER}\n"
        f"# Source checkout: {ROOT}\n\n"
        "schemaVersion = 4\n\n"
        "[environment.root]\n"
        f"GOAL_PLUS_SOURCE_PATH = {json.dumps(str(ROOT))}\n",
        encoding="utf-8",
    )
    base = installer._render_thinkthread_profile(_thinkthread_pi_profile())

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "thinkthread 0.1.0\n" if "--version" in argv else base
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(installer, "_run", run)

    plan = installer._preflight_thinkthread_profile(
        "/opt/bin/tt",
        profile_path=profile_path,
    )

    assert plan.installed is False
    assert plan.content != profile_path.read_text(encoding="utf-8")


def test_thinkthread_profile_dry_run_does_not_write(
    installer: ModuleType,
    tmp_path: Path,
) -> None:
    path = tmp_path / "goal-plus-pi.toml"
    plan = installer.ThinkThreadProfilePlan(
        executable="/opt/bin/tt",
        runtime_version="thinkthread 0.1.0",
        path=path,
        content="profile\n",
        installed=False,
    )

    installer._install_thinkthread_profile(plan, dry_run=True)

    assert not path.exists()


def test_thinkthread_sdk_preflight_reuses_only_owned_valid_install(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "sdk"
    entry = (
        root
        / "node_modules"
        / "@thinkthread"
        / "agent-posix"
        / "dist"
        / "index.js"
    )
    entry.parent.mkdir(parents=True)
    entry.write_text("export {};\n", encoding="utf-8")
    (root / "installation.json").write_text(
        json.dumps(
            {
                "schema": installer.THINKTHREAD_SDK_RECEIPT_SCHEMA,
                "source_path": str(ROOT),
            }
        ),
        encoding="utf-8",
    )
    validated: list[Path] = []
    monkeypatch.setattr(
        installer,
        "_validate_agent_posix_sdk",
        lambda _node, path: validated.append(path),
    )

    plan = installer._preflight_thinkthread_sdk(
        node_executable="/opt/node",
        npm_executable="/opt/npm",
        git_executable="/opt/git",
        install_root=root,
    )

    assert plan.installed is True
    assert plan.entry == entry
    assert validated == [entry]


def test_thinkthread_sdk_preflight_refuses_unmanaged_directory(
    installer: ModuleType,
    tmp_path: Path,
) -> None:
    root = tmp_path / "sdk"
    root.mkdir()

    with pytest.raises(installer.InstallError, match="not Goal Plus-managed"):
        installer._preflight_thinkthread_sdk(
            node_executable="/opt/node",
            npm_executable="/opt/npm",
            git_executable="/opt/git",
            install_root=root,
        )
