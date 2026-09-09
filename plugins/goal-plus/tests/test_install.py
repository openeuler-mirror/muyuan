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
def installer(tmp_path, monkeypatch) -> ModuleType:
    monkeypatch.setenv("GOAL_PLUS_INSTALL_HOME", str(tmp_path / "managed"))
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


def test_pi_normal_rejects_same_checkout_loose_resource(
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

    with pytest.raises(installer.InstallError, match="conflicting Goal Plus"):
        installer._preflight_pi_normal()


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

    monkeypatch.setattr(installer, "_codex_marketplaces", lambda _codex: {"goal-plus": ROOT})
    monkeypatch.setattr(installer.managed_runtime, "bind", lambda *args, **kwargs: None)
    verified = []
    monkeypatch.setattr(installer, "_verify_codex_installation", lambda codex, package: verified.append(package))
    installer._install_codex(
        codex="codex",
        package=ROOT,
        dry_run=False,
    )

    assert commands == [["codex", "plugin", "add", "goal-plus@goal-plus"]]
    assert verified == [ROOT]


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
            runtime_python=tmp_path / "venv/bin/python",
    )
    assert first.installed is False
    profile_path.write_text(first.content, encoding="utf-8")

    second = installer._preflight_thinkthread_profile(
        "/opt/bin/tt",
        profile_path=profile_path,
            runtime_python=tmp_path / "venv/bin/python",
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
            runtime_python=tmp_path / "venv/bin/python",
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
            runtime_python=tmp_path / "venv/bin/python",
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


@pytest.mark.parametrize("failure", ["registration", "cache"])
def test_codex_update_failure_restores_previous_source_and_binding(installer, tmp_path, monkeypatch, failure):
    old, new = tmp_path / "old", tmp_path / "new"
    current = {"goal-plus": old}
    installer.managed_runtime.bind("codex", installer._codex_config(), old)
    installer.managed_runtime.bind("pi", installer._pi_agent_dir(), old)
    monkeypatch.setattr(installer, "_codex_marketplaces", lambda _: dict(current))
    def run(argv, **kwargs):
        if argv[1:4] == ["plugin", "marketplace", "remove"]:
            current.clear()
        elif argv[1:4] == ["plugin", "marketplace", "add"]:
            current["goal-plus"] = Path(argv[4])
        elif current.get("goal-plus") == new and failure == "registration":
            raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(installer, "_run", run)
    def reject_cache(codex, package):
        raise installer.InstallError("Codex cache mismatch")
    monkeypatch.setattr(installer, "_verify_codex_installation", reject_cache)
    with pytest.raises(subprocess.CalledProcessError if failure == "registration" else installer.InstallError):
        installer._install_codex(codex="codex", package=new, dry_run=False)
    assert current == {"goal-plus": old}
    for host, config in [("codex", installer._codex_config()), ("pi", installer._pi_agent_dir())]:
        assert json.loads(installer.managed_runtime.binding_path(host, config).read_text())["package"] == str(old)
    assert not installer.managed_runtime.binding_path("codex-pending", installer._codex_config()).exists()


@pytest.fixture
def codex_release(installer, tmp_path, monkeypatch):
    import shutil

    package = tmp_path / "release/package"
    config = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(config))
    build = "a" * 64
    version = "0.1.0+" + build
    receipt = {"schema": installer.managed_runtime.SCHEMA, "build": build,
               "package": str(package), "python": str(tmp_path / "release/venv/bin/python")}
    launcher = package / "scripts/run-runtime.sh"
    documents = {
        ".codex-plugin/plugin.json": {"name": "goal-plus", "version": version},
        ".goal-plus-runtime.json": receipt,
        ".mcp.json": {"mcpServers": {"goal-plus": {"command": "bash", "args": [str(launcher), "--root", ".gp"]}}},
        "hooks/hooks.json": {"hooks": {"Stop": [{"hooks": [{"command": f"bash {launcher} --goal-plus-host-hook"}]}]}},
    }
    for name, value in documents.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    (package / ".goal-plus-build").write_text(build + "\n")
    (package / ".goal-plus-python").write_text(receipt["python"] + "\n")
    cache = config / "plugins/cache/goal-plus/goal-plus" / version
    shutil.copytree(package, cache)
    item = {"pluginId": "goal-plus@goal-plus", "installed": True, "enabled": True,
            "version": version, "source": {"source": "local", "path": str(package)}}
    monkeypatch.setattr(installer, "_codex_inventory", lambda _: [item])
    monkeypatch.setattr(installer, "_codex_marketplaces", lambda _: {"goal-plus": package})
    return package, cache, item


def test_codex_installation_checks_actual_cache(installer, codex_release):
    package, _, _ = codex_release
    installer._verify_codex_installation("codex", package)


@pytest.mark.parametrize("corruption", ["mcp", "build"])
def test_codex_first_install_cache_failure_removes_new_registration(installer, codex_release, monkeypatch, corruption):
    package, cache, item = codex_release
    if corruption == "mcp":
        (cache / ".mcp.json").write_bytes((ROOT / ".mcp.json").read_bytes())
    else:
        (cache / ".goal-plus-build").write_bytes(b"\xff")
    marketplaces = {}
    installed = []
    monkeypatch.setattr(installer, "_codex_marketplaces", lambda _: dict(marketplaces))
    monkeypatch.setattr(installer, "_codex_inventory", lambda _: list(installed))
    def run(argv, **kwargs):
        if argv[1:4] == ["plugin", "marketplace", "add"]:
            marketplaces["goal-plus"] = Path(argv[4])
        elif argv[1:4] == ["plugin", "marketplace", "remove"]:
            marketplaces.clear()
        elif argv[1:3] == ["plugin", "add"]:
            installed.append(item)
        elif argv[1:3] == ["plugin", "remove"]:
            installed.clear()
        else:
            pytest.fail(f"unexpected command: {argv}")
    monkeypatch.setattr(installer, "_run", run)
    with pytest.raises(installer.InstallError, match="Codex cache mismatch"):
        installer._install_codex(codex="codex", package=package, dry_run=False)
    assert installed == []
    assert marketplaces == {}
    assert package.is_dir()
    for host in ("codex", "codex-pending"):
        assert not installer.managed_runtime.binding_path(host, installer._codex_config()).exists()


@pytest.mark.parametrize("corruption", ["version", "source", "disabled", "manifest", "receipt", "mcp", "hooks", "build", "python", "missing"])
def test_codex_installation_rejects_stale_or_mismatched_cache(installer, codex_release, corruption):
    package, cache, item = codex_release
    if corruption == "version":
        item["version"] = "0.1.0"
    elif corruption == "source":
        item["source"]["path"] = str(ROOT)
    elif corruption == "disabled":
        item["enabled"] = False
    elif corruption == "manifest":
        (cache / ".codex-plugin/plugin.json").write_text('{"version":"0.1.0"}')
    elif corruption == "receipt":
        (cache / ".goal-plus-runtime.json").write_text('{}')
    elif corruption == "mcp":
        (cache / ".mcp.json").write_bytes((ROOT / ".mcp.json").read_bytes())
    elif corruption == "hooks":
        (cache / "hooks/hooks.json").write_bytes((ROOT / "hooks/hooks.json").read_bytes())
    elif corruption in {"build", "python"}:
        (cache / f".goal-plus-{corruption}").write_text("wrong\n")
    else:
        (cache / ".goal-plus-build").unlink()
    with pytest.raises(installer.InstallError, match="Codex"):
        installer._verify_codex_installation("codex", package)


def test_codex_dry_run_does_not_verify_unmaterialized_cache(installer, tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "_codex_marketplaces", lambda _: {})
    monkeypatch.setattr(installer, "_run", lambda *args, **kwargs: None)
    def unexpected(*args):
        pytest.fail("dry-run must not verify a cache that has not been installed")
    monkeypatch.setattr(installer, "_verify_codex_installation", unexpected)
    installer._install_codex(codex="codex", package=tmp_path / "missing", dry_run=True)
    assert not installer.managed_runtime.home().exists()


@pytest.mark.parametrize("fail", [True, False])
def test_pi_update_preserves_filters_and_recovers_settings(installer, tmp_path, monkeypatch, fail):
    agent = tmp_path / "pi"
    old, new = tmp_path / "old", tmp_path / "new"
    original = {"packages": [{"source": "../old", "extensions": ["*.ts"]}, "npm:unrelated"], "theme": "dark"}
    _write_pi_settings(agent, original["packages"])
    (agent / "settings.json").write_text(json.dumps(original))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    monkeypatch.setattr(installer, "_pi_goal_plus_entries", lambda: [{"source": "../old", "resolved_path": old, "settings_path": agent / "settings.json"}])
    installer.managed_runtime.bind("pi", agent, old)
    def run(argv, **kwargs):
        if argv[1] == "remove":
            assert argv[2] == str(old)
            (agent / "settings.json").write_text(json.dumps({"packages": ["npm:unrelated"], "theme": "dark"}))
        elif fail:
            raise subprocess.CalledProcessError(1, argv)
        else:
            (agent / "settings.json").write_text(json.dumps({"packages": ["npm:unrelated", str(new)], "theme": "dark"}))
    monkeypatch.setattr(installer, "_run", run)
    if fail:
        with pytest.raises(subprocess.CalledProcessError):
            installer._install_pi(pi="pi", package=new, dry_run=False)
        assert json.loads((agent / "settings.json").read_text()) == original
    else:
        installer._install_pi(pi="pi", package=new, dry_run=False)
        result = json.loads((agent / "settings.json").read_text())
        assert result == {**original, "packages": [{"source": str(new), "extensions": ["*.ts"]}, "npm:unrelated"]}
    assert json.loads(installer.managed_runtime.binding_path("pi", agent).read_text())["package"] == str(old if fail else new)


def test_python_symlink_chain_can_revisit_directory_with_different_suffix(installer, tmp_path):
    real = tmp_path / "real"
    (real / "env/bin").mkdir(parents=True)
    (real / "bin").mkdir()
    (real / "bin/python3").touch()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    (real / "env/bin/python").symlink_to(alias / "bin/python3")
    roots = installer._symlink_chain_parent_roots(alias / "env/bin/python")
    assert real / "env/bin" in roots


def test_thinkthread_profile_uses_managed_package_python_and_retains_source_owner(installer, tmp_path):
    package = tmp_path / "release/package"
    package.mkdir(parents=True)
    (package / ".goal-plus-runtime.json").write_text(json.dumps({"schema": installer.managed_runtime.SCHEMA, "source": str(ROOT)}))
    python = tmp_path / "release/venv/bin/python"
    profile = installer._build_thinkthread_profile(_thinkthread_pi_profile(), package_root=package,
                                                  runtime_python=python, node_executable="/opt/node/bin/node")
    assert str(package / ".pi/extensions/goal-plus.ts") in profile["agent"]["args"]
    assert profile["environment"]["root"]["GOAL_PLUS_PYTHON"] == str(python)
    assert str(python.parent.parent) in profile["environment"]["read"]
    path = tmp_path / "profile.toml"
    path.write_text(installer._render_thinkthread_profile(profile))
    assert installer._managed_thinkthread_profile_source(path) == ROOT


def test_pi_cli_removal_keeps_lexical_alias(installer, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    entry = {"source": "../package", "resolved_path": real / "package", "settings_path": alias / "agent/settings.json"}
    assert installer._pi_cli_source(entry) == str(alias / "package")
