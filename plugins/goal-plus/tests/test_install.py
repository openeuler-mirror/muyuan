from __future__ import annotations

import importlib.util
import json
import os
import shutil
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


def test_codex_provider_credentials_forward_only_configured_names(installer, tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("UNRELATED_TOKEN", "private-unrelated")
    monkeypatch.setenv("MODEL_TOKEN", "private-provider")
    (home / "config.toml").write_text(
        '[model_providers.custom]\nenv_key="MODEL_TOKEN"\n'
        'base_url="https://example.invalid"\n'
        '[model_providers.custom.env_http_headers]\nAuthorization="HEADER_TOKEN"\n'
    )
    assert installer._codex_provider_env_vars() == ("HEADER_TOKEN", "MODEL_TOKEN", "OPENAI_API_KEY")


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


def test_inspect_pi_resolves_public_api_from_bundled_cli(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required")
    pi_package = tmp_path / "node_modules" / "@earendil-works" / "pi-coding-agent"
    cli = pi_package / "dist" / "bundle" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    (pi_package / "package.json").write_text(
        json.dumps(
            {
                "name": "@earendil-works/pi-coding-agent",
                "type": "module",
                "exports": {".": {"import": "./dist/index.js"}},
            }
        ),
        encoding="utf-8",
    )
    (pi_package / "dist" / "index.js").write_text(
        """
export const SettingsManager = {
  create(cwd, agentDir, options) { return {cwd, agentDir, options}; },
};
export class DefaultPackageManager {
  constructor({settingsManager}) {
    this.settingsManager = settingsManager;
  }
  async resolve() {
    return {
      extensions: [{path: this.settingsManager.cwd + "/goal-plus.ts", enabled: true}],
      skills: [{path: this.settingsManager.agentDir + "/goal-plus/SKILL.md", enabled: true}],
    };
  }
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    agent_dir = tmp_path / "agent"
    cwd = tmp_path / "workspace"

    result = subprocess.run(
        [node, str(ROOT / "scripts" / "inspect-pi.mjs"), str(cli), str(agent_dir), str(cwd)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "extensions": [{"path": str(cwd / "goal-plus.ts"), "enabled": True}],
        "skills": [{"path": str(agent_dir / "goal-plus/SKILL.md"), "enabled": True}],
    }


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
    assert str(ROOT / "assets" / "pi" / "extensions" / "goal-plus.ts") in launcher
    assert "--skill" not in launcher
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


def test_pi_dev_isolated_from_retired_source_package_registration(
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
    _write_pi_settings(agent_dir, [])
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


def test_pi_dev_ignores_stale_local_goal_plus_path(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _write_pi_settings(agent_dir, ["../missing-checkout/goal-plus"])
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    installer._preflight_pi_dev()


def test_pi_dev_isolated_from_project_local_goal_plus_assets(
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
    extension = agent_dir / "extensions" / "goal-plus.ts"
    extension.parent.mkdir(parents=True)
    extension.symlink_to(ROOT / "assets" / "pi" / "extensions" / "goal-plus.ts")
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


def test_thinkthread_codex_install_flow_installs_config_before_profile(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(installer, "_require_executable", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(installer, "_preflight_codex", lambda *a, **k: False)
    monkeypatch.setattr(installer, "_codex_inventory", lambda _codex: [])
    monkeypatch.setattr(installer, "_install_codex", lambda **kwargs: calls.append("codex"))
    package = tmp_path / "release" / "package"
    monkeypatch.setattr(installer.managed_runtime, "prepare", lambda *a, **k: package)
    monkeypatch.setattr(installer, "_preflight_thinkthread_sdk", lambda **kwargs: None)
    config_plan = installer.ThinkThreadCodexConfigPlan(tmp_path / "managed-config", "content", None)
    monkeypatch.setattr(
        installer, "_plan_thinkthread_codex_config",
        lambda _package, **_kwargs: calls.append("plan_config") or config_plan,
    )
    seen: dict[str, dict] = {}

    def preflight(_executable, **kwargs):
        agent = kwargs.get("native_agent", "pi")
        seen[agent] = kwargs
        return installer.ThinkThreadProfilePlan(
            executable="/opt/bin/tt", runtime_version="thinkthread 0.1.0",
            path=tmp_path / f"goal-plus-{agent}.toml", content="content",
            installed=False, native_agent=agent,
        )

    monkeypatch.setattr(installer, "_preflight_thinkthread_profile", preflight)
    monkeypatch.setattr(installer, "_install_thinkthread_sdk", lambda plan, *, dry_run: calls.append("sdk"))
    monkeypatch.setattr(
        installer, "_install_thinkthread_codex_config",
        lambda plan, *, dry_run: calls.append("config"),
    )
    monkeypatch.setattr(
        installer, "_install_thinkthread_profile",
        lambda plan, *, dry_run: calls.append(f"profile:{plan.native_agent}"),
    )
    monkeypatch.setattr(
        installer.managed_runtime, "bind",
        lambda host, path, _package, *, dry_run: calls.append(f"bind:{path.stem}"),
    )

    assert installer.main(["--codex", "--thinkthread", "-y"]) == 0

    assert "pi" not in seen
    assert seen["codex"]["codex_config"] is config_plan
    assert calls == ["plan_config", "codex", "config", "profile:codex", "bind:goal-plus-codex"]


def test_thinkthread_pi_install_flow_skips_the_codex_profile(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer, "_require_executable", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(installer, "_preflight_pi_normal", lambda **kwargs: True)
    monkeypatch.setattr(installer, "_pi_goal_plus_entries", lambda: [])
    monkeypatch.setattr(installer, "_install_pi", lambda **kwargs: None)
    package = tmp_path / "release" / "package"
    monkeypatch.setattr(installer.managed_runtime, "prepare", lambda *a, **k: package)
    monkeypatch.setattr(installer, "_preflight_thinkthread_sdk", lambda **kwargs: None)
    monkeypatch.setattr(
        installer, "_plan_thinkthread_codex_config",
        lambda _package, **_kwargs: pytest.fail("codex config planned for --pi"),
    )
    seen: dict[str, dict] = {}

    def preflight(_executable, **kwargs):
        agent = kwargs.get("native_agent", "pi")
        seen[agent] = kwargs
        return installer.ThinkThreadProfilePlan(
            executable="/opt/bin/tt", runtime_version="thinkthread 0.1.0",
            path=tmp_path / f"goal-plus-{agent}.toml", content="content",
            installed=False, native_agent=agent,
        )

    monkeypatch.setattr(installer, "_preflight_thinkthread_profile", preflight)
    monkeypatch.setattr(installer, "_install_thinkthread_profile", lambda plan, *, dry_run: None)
    monkeypatch.setattr(installer.managed_runtime, "bind", lambda *a, **k: None)

    assert installer.main(["--pi", "--thinkthread", "-y"]) == 0

    assert list(seen) == ["pi"]


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
        str(ROOT / "assets" / "pi" / "extensions" / "goal-plus.ts"),
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


def test_thinkthread_codex_profile_preserves_native_extension_and_binds_managed_config(installer, tmp_path):
    base = _thinkthread_pi_profile()
    base["agent"]["name"] = "codex"
    base["agent"]["extension"]["name"] = "codex"
    base["agent"]["extension"]["loader"]["command"] = ["thinkthread-extension-codex"]
    config = tmp_path / "managed-config"
    profile = installer._build_thinkthread_profile(
        base, node_executable=str(tmp_path / "node/bin/node"),
        runtime_python=tmp_path / "venv/bin/python", codex_config_dir=config,
    )
    assert profile["agent"]["extension"] == base["agent"]["extension"]
    assert profile["agent"]["args"] == base["agent"].get("args", [])
    assert profile["agent"]["configDirEnv"] == "CODEX_CONFIG_DIR"
    assert profile["agent"]["configDirDefault"] == str(config)
    assert profile["agent"]["configDirAccess"] == "read_only"
    assert profile["environment"]["child"]["GOAL_PLUS_THINKTHREAD_ROLE"] == "candidate"
    assert profile["environment"]["root"]["GOAL_PLUS_THINKTHREAD_ROLE"] == "main"
    assert profile["environment"]["root"]["GOAL_PLUS_ROOT"] == "${CODEX_HOME}/goal-plus"
    assert "GOAL_PLUS_PI_ROLE" not in profile["environment"]["child"]
    with pytest.raises(installer.InstallError, match="managed absolute source config"):
        installer._build_thinkthread_profile(base)


def test_thinkthread_codex_config_isolates_native_tools_and_forwards_only_env_names(installer, tmp_path):
    source = {
        "model": "provider-model", "model_provider": "custom",
        "model_catalog_json": "/catalog/models.json",
        "model_providers": {"custom": {"env_key": "CUSTOM_MODEL_KEY", "base_url": "https://example.invalid"}},
        "developer_instructions": "Main-only instructions", "plugins": {"private": {"enabled": True}},
        "mcp_servers": {"private": {"command": "private-server"}},
    }
    config = installer._build_thinkthread_codex_config(source, runtime_python=tmp_path / "venv/bin/python")
    assert config["model_providers"] == source["model_providers"]
    assert config["model"] == source["model"]
    assert config["model_catalog_json"] == source["model_catalog_json"]
    assert set(config["mcp_servers"]) == {"goal-plus"}
    assert "developer_instructions" not in config and "plugins" not in config
    mcp = config["mcp_servers"]["goal-plus"]
    assert mcp["args"] == ["-I", "-m", "goal_plus.server"]
    assert "THINKTHREAD_CONTROL_TOKEN" in mcp["env_vars"]
    assert "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED" in mcp["env_vars"]
    assert "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED" in mcp["env_vars"]
    assert "env" not in mcp
    assert source["mcp_servers"] == {"private": {"command": "private-server"}}
    assert installer.tomllib.loads(installer._render_thinkthread_profile(config)) == config


def test_thinkthread_codex_config_plan_keeps_auth_as_an_owned_file_reference(installer, tmp_path):
    source = tmp_path / "codex-config"
    source.mkdir()
    (source / "config.toml").write_text('model = "native-model"\n')
    auth = source / "auth.json"
    auth.write_text('{"fixture":"not-a-credential"}')
    package = tmp_path / "release/package"
    plan = installer._plan_thinkthread_codex_config(package, source)
    assert not plan.directory.exists()
    assert plan.auth_source == auth
    installer._install_thinkthread_codex_config(plan, dry_run=True)
    assert not plan.directory.exists()
    installer._install_thinkthread_codex_config(plan, dry_run=False)
    assert (plan.directory / "auth.json").is_symlink()
    assert (plan.directory / "auth.json").resolve() == auth
    assert (plan.directory / "config.toml").stat().st_mode & 0o777 == 0o600
    installer._install_thinkthread_codex_config(plan, dry_run=False)
    assert auth.read_text() == '{"fixture":"not-a-credential"}'
    (source / "config.toml").write_text('model = "different-model"\n')
    assert installer._plan_thinkthread_codex_config(package, source).directory != plan.directory
    (plan.directory / ".goal-plus-owner.json").unlink()
    with pytest.raises(installer.InstallError, match="unowned"):
        installer._install_thinkthread_codex_config(plan, dry_run=False)


def test_thinkthread_codex_config_plan_overlays_the_codex_profile_layer(installer, tmp_path):
    source = tmp_path / "codex-config"
    source.mkdir()
    (source / "config.toml").write_text(
        'model = "gpt-6-astra"\nmodel_provider = "sub2api-openai"\n'
        '[model_providers.sub2api-openai]\nname = "Sub2API OpenAI"\nbase_url = "http://127.0.0.1:3788/v1"\n'
    )
    (source / "deepseek.config.toml").write_text(
        'model = "deepseek-flash"\nmodel_provider = "deepseek"\n'
        'model_reasoning_effort = "high"\n'
        f'model_catalog_json = "{source / "models.json"}"\n'
        '[model_providers.deepseek]\nname = "DeepSeek"\nwire_api = "responses"\n'
    )
    (source / "models.json").write_text('{"models": {}}\n')
    package = tmp_path / "release/package"
    layered = installer._plan_thinkthread_codex_config(package, source, profile="deepseek")
    parsed = installer.tomllib.loads(layered.content)
    assert parsed["model"] == "deepseek-flash"
    assert parsed["model_provider"] == "deepseek"
    assert parsed["model_reasoning_effort"] == "high"
    assert parsed["model_catalog_json"] == str((source / "models.json").resolve())
    assert layered.catalog_source == (source / "models.json").resolve()
    # A symlinked config home must project the resolved catalog path: the Child
    # sandbox only exposes whitelisted paths, not symlink aliases.
    alias = tmp_path / "codex-alias"
    alias.symlink_to(source, target_is_directory=True)
    (source / "deepseek.config.toml").write_text(
        (source / "deepseek.config.toml").read_text().replace(str(source), str(alias))
    )
    canonical = installer._plan_thinkthread_codex_config(package, source, profile="deepseek")
    assert installer.tomllib.loads(canonical.content)["model_catalog_json"] == str(
        (source / "models.json").resolve()
    )
    assert "sub2api-openai" in parsed["model_providers"] and "deepseek" in parsed["model_providers"]
    assert layered.directory != installer._plan_thinkthread_codex_config(package, source).directory
    with pytest.raises(installer.InstallError, match="profile config not found"):
        installer._plan_thinkthread_codex_config(package, source, profile="absent")
    (source / "models.json").unlink()
    with pytest.raises(installer.InstallError, match="model catalog JSON is unavailable"):
        installer._plan_thinkthread_codex_config(package, source, profile="deepseek")


def test_resolve_system_ca_bundles_follows_symlinked_candidates(installer, tmp_path):
    trust = tmp_path / "pki" / "ca-trust" / "extracted" / "pem"
    trust.mkdir(parents=True)
    (trust / "tls-ca-bundle.pem").write_text("roots\n")
    certs = tmp_path / "ssl" / "certs"
    certs.mkdir(parents=True)
    (certs / "ca-bundle.crt").symlink_to(trust / "tls-ca-bundle.pem")

    resolved = installer._resolve_system_ca_bundles((
        str(certs / "ca-certificates.crt"),
        str(certs / "ca-bundle.crt"),
        str(trust / "tls-ca-bundle.pem"),
    ))

    assert resolved == [trust / "tls-ca-bundle.pem"]


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


@pytest.mark.parametrize("protocol", [None, 5, 6])
def test_thinkthread_profile_accepts_optional_protocol_declaration(installer, capsys, protocol):
    profile = _thinkthread_pi_profile()
    loader = profile["agent"]["extension"]["loader"]
    if protocol is None:
        loader.pop("protocolVersion")
    else:
        loader["protocolVersion"] = protocol
    installer._validate_base_thinkthread_profile(profile)
    assert ("warning:" in capsys.readouterr().err) is (protocol == 6)


@pytest.mark.parametrize("mode", ["rootFsMode", "childFsMode"])
def test_thinkthread_profile_still_rejects_incompatible_filesystem(installer, mode):
    profile = _thinkthread_pi_profile()
    profile["environment"][mode] = "unsupported"
    with pytest.raises(installer.InstallError, match="rootFsMode=direct"):
        installer._validate_base_thinkthread_profile(profile)


def test_thinkthread_profile_new_version_warns_and_prepares_install(installer, tmp_path, monkeypatch, capsys):
    profile = _thinkthread_pi_profile()
    profile["schemaVersion"] = 5
    profile["agent"]["extension"]["loader"].pop("protocolVersion")
    monkeypatch.setattr(installer, "_run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout="thinkthread 0.2.0\n" if "--version" in argv else installer._render_thinkthread_profile(profile),
    ))
    plan = installer._preflight_thinkthread_profile(
        "/opt/bin/tt", profile_path=tmp_path / "goal-plus-pi.toml", node_executable="/opt/node/bin/node",
    )
    assert plan.runtime_version == "thinkthread 0.2.0"
    assert installer._parse_thinkthread_profile(plan.content, source="test")["schemaVersion"] == 5
    assert "warning:" in capsys.readouterr().err


def test_thinkthread_sdk_metadata_difference_warns(installer, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(installer, "_run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps({"protocol": 3, "fingerprint": "new-contract"}),
    ))
    installer._validate_agent_posix_sdk("node", tmp_path / "index.js")
    assert "warning:" in capsys.readouterr().err


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


def test_thinkthread_profile_preflight_allows_explicit_replacement(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path = tmp_path / "goal-plus-pi.toml"
    profile_path.write_text("schemaVersion = 4\n", encoding="utf-8")
    base = installer._render_thinkthread_profile(_thinkthread_pi_profile())

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "thinkthread 0.1.0\n" if "--version" in argv else base
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(installer, "_run", run)

    plan = installer._preflight_thinkthread_profile(
        "/opt/bin/tt",
        profile_path=profile_path,
        runtime_python=tmp_path / "venv/bin/python",
        allow_replacement=True,
    )

    assert plan.installed is False
    assert "replacing unmanaged ThinkThread Profile" in capsys.readouterr().err


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


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["command", "parse", "validation", "interrupt"])
def test_thinkthread_profile_validation_failure_restores_previous_file(
    installer, tmp_path, monkeypatch, existing, failure,
):
    path = tmp_path / "goal-plus-pi.toml"
    original = b"# original profile\r\nschemaVersion = 4\r\n"
    if existing:
        path.write_bytes(original)
        path.chmod(0o600)
    content = installer._render_thinkthread_profile(_thinkthread_pi_profile())
    plan = installer.ThinkThreadProfilePlan(
        executable="/opt/bin/tt", runtime_version="thinkthread 0.1.0",
        path=path, content=content, installed=False,
    )

    def run(argv, **kwargs):
        assert path.read_text() == content
        if failure == "command":
            raise subprocess.CalledProcessError(1, argv)
        if failure == "interrupt":
            raise KeyboardInterrupt()
        stdout = "invalid = [" if failure == "parse" else "schemaVersion = 0\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    monkeypatch.setattr(installer, "_run", run)
    expected = {
        "command": subprocess.CalledProcessError,
        "parse": installer.InstallError,
        "validation": installer.InstallError,
        "interrupt": KeyboardInterrupt,
    }[failure]
    with pytest.raises(expected):
        installer._install_thinkthread_profile(plan, dry_run=False)

    if existing:
        assert path.read_bytes() == original
        assert path.stat().st_mode & 0o777 == 0o600
    else:
        assert not path.exists()


@pytest.mark.parametrize("existing", [False, True])
def test_thinkthread_profile_success_keeps_new_file(installer, tmp_path, monkeypatch, existing):
    path = tmp_path / "goal-plus-pi.toml"
    if existing:
        path.write_text("# old profile\n")
    content = installer._render_thinkthread_profile(_thinkthread_pi_profile())
    plan = installer.ThinkThreadProfilePlan(
        executable="/opt/bin/tt", runtime_version="thinkthread 0.1.0",
        path=path, content=content, installed=False,
    )
    monkeypatch.setattr(installer, "_run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout=path.read_text(),
    ))

    installer._install_thinkthread_profile(plan, dry_run=False)

    assert path.read_text() == content


def test_thinkthread_profile_rollback_failure_reports_both_errors(installer, tmp_path, monkeypatch):
    path = tmp_path / "goal-plus-pi.toml"
    path.write_text("# old profile\n")
    plan = installer.ThinkThreadProfilePlan(
        executable="/opt/bin/tt", runtime_version="thinkthread 0.1.0",
        path=path, content="# new profile\n", installed=False,
    )

    def fail_restore(*args, **kwargs):
        raise OSError("restore denied")

    def run(argv, **kwargs):
        monkeypatch.setattr(installer, "_atomic_write", fail_restore)
        raise installer.InstallError("profile validation failed")

    monkeypatch.setattr(installer, "_run", run)
    with pytest.raises(installer.InstallError) as raised:
        installer._install_thinkthread_profile(plan, dry_run=False)

    assert str(path) in str(raised.value)
    assert "profile validation failed" in str(raised.value)
    assert "restore denied" in str(raised.value)


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


def test_thinkthread_sdk_preflight_allows_explicit_replacement(
    installer: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "sdk"
    root.mkdir()

    plan = installer._preflight_thinkthread_sdk(
        node_executable="/opt/node",
        npm_executable="/opt/npm",
        git_executable="/opt/git",
        install_root=root,
        allow_replacement=True,
    )

    assert plan.installed is False
    assert "replacing unmanaged Agent POSIX SDK" in capsys.readouterr().err


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
        (cache / ".mcp.json").write_bytes((ROOT / "assets/codex/mcp.json").read_bytes())
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
        (cache / ".mcp.json").write_bytes((ROOT / "assets/codex/mcp.json").read_bytes())
    elif corruption == "hooks":
        (cache / "hooks/hooks.json").write_bytes((ROOT / "assets/codex/hooks.json").read_bytes())
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
    monkeypatch.setattr(installer, "_verify_pi_installation", lambda *args: None)
    agent = tmp_path / "pi"
    old, new = tmp_path / "old", tmp_path / "new"
    original = {"packages": [{"source": "../old", "extensions": ["*.ts"]}, "npm:unrelated"], "theme": "dark"}
    _write_pi_settings(agent, original["packages"])
    (agent / "settings.json").write_text(json.dumps(original))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    monkeypatch.setattr(installer, "_pi_goal_plus_entries", lambda: [{"source": "../old", "resolved_path": old, "settings_path": agent / "settings.json"}])
    installer.managed_runtime.bind("pi", agent, old)
    def run(argv, **kwargs):
        if argv[1] == "ci":
            return
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


def test_pi_mcp_dependency_failure_preserves_registration(installer, tmp_path, monkeypatch):
    agent = tmp_path / "pi"
    old, new = tmp_path / "old", tmp_path / "new"
    _write_pi_settings(agent, [str(old)])
    settings = agent / "settings.json"
    before = settings.read_bytes()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    installer.managed_runtime.bind("pi", agent, old)

    def fail_dependency(argv, **kwargs):
        assert argv[1] == "ci"
        assert "--ignore-scripts" in argv
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(installer, "_run", fail_dependency)
    with pytest.raises(subprocess.CalledProcessError):
        installer._install_pi(pi="pi", package=new, dry_run=False)
    assert settings.read_bytes() == before
    assert json.loads(installer.managed_runtime.binding_path("pi", agent).read_text())["package"] == str(old)
    assert not installer.managed_runtime.binding_path("pi-pending", agent).exists()


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


def test_thinkthread_profile_uses_managed_package_python_and_retains_source_owner(installer, tmp_path, monkeypatch):
    package = tmp_path / "release/package"
    package.mkdir(parents=True)
    (package / ".goal-plus-runtime.json").write_text(json.dumps({"schema": installer.managed_runtime.SCHEMA, "source": str(ROOT)}))
    python = tmp_path / "release/venv/bin/python"
    python.parent.mkdir(parents=True)
    base = tmp_path / "managed-python/cpython-3.12/bin/python3.12"
    base.parent.mkdir(parents=True)
    base.touch()
    alias = tmp_path / "managed-python/python-alias"
    alias.symlink_to(base.parent.parent, target_is_directory=True)
    python.symlink_to(alias / "bin/python3.12")
    host = tmp_path / "foreign-venv"
    monkeypatch.setattr(installer.sys, "executable", str(host / "bin/python"))
    monkeypatch.setattr(installer.sys, "prefix", str(host))
    profile = installer._build_thinkthread_profile(_thinkthread_pi_profile(), package_root=package,
                                                  runtime_python=python, node_executable="/opt/node/bin/node")
    assert str(package / "assets/pi/extensions/goal-plus.ts") in profile["agent"]["args"]
    assert profile["environment"]["root"]["GOAL_PLUS_PYTHON"] == str(python)
    assert str(python.parent.parent) in profile["environment"]["read"]
    roots = [Path(path) for path in profile["environment"]["read"]]
    for required in (python, alias / "bin/python3.12", base, base.parent.parent / "lib"):
        assert any(required.is_relative_to(root) for root in roots)
    assert not any(host.is_relative_to(root) for root in roots)
    assert profile["environment"]["child"]["GOAL_PLUS_PYTHON"] == str(python)
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


@pytest.mark.parametrize("answer, expected", [("y", True), ("YES", True), ("n", False), ("", False), (None, False)])
def test_reinstall_confirmation_defaults_to_cancel(installer, monkeypatch, answer, expected):
    def respond(_prompt):
        if answer is None:
            raise EOFError
        return answer
    monkeypatch.setattr("builtins.input", respond)
    assert installer._confirm_reinstall(["Pi: old"], assume_yes=False, dry_run=False) is expected


@pytest.mark.parametrize("option", ["--yes", "--dry-run"])
def test_reinstall_automation_never_reads_stdin(installer, monkeypatch, option):
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("unexpected prompt"))
    assert installer._confirm_reinstall(["Pi: old"], assume_yes=option == "--yes", dry_run=option == "--dry-run")


def test_cancel_reinstall_does_not_prepare_or_mutate(installer, monkeypatch):
    monkeypatch.setattr(installer, "_require_executable", lambda _: "pi")
    monkeypatch.setattr(installer, "_preflight_pi_normal", lambda **kwargs: True)
    monkeypatch.setattr(installer, "_pi_goal_plus_entries", lambda: [{"source": "old"}])
    monkeypatch.setattr("builtins.input", lambda _: "n")
    monkeypatch.setattr(installer.managed_runtime, "prepare", lambda *a, **k: pytest.fail("prepared after cancellation"))
    assert installer.main(["--pi"]) == 0
    assert not installer.managed_runtime.home().exists()


@pytest.mark.parametrize("failure", ["invalid_json", "invalid_shape", "runtime", "missing_registration"])
def test_pi_post_install_failure_restores_settings_and_binding(installer, tmp_path, monkeypatch, failure):
    agent = tmp_path / "agent"
    old = tmp_path / "goal-plus"
    new = tmp_path / "release/package"
    original = json.dumps({"packages": [str(old)], "theme": "dark"})
    _write_pi_settings(agent, [])
    settings = agent / "settings.json"
    settings.write_text(original)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    installer.managed_runtime.bind("pi", agent, old)
    def run(argv, **kwargs):
        if argv[1] == "ci":
            return
        value = {"invalid_json": "{", "invalid_shape": "[]"}.get(failure, '{"packages": []}')
        settings.write_text(value if argv[1] == "install" else '{"packages": []}')
    monkeypatch.setattr(installer, "_run", run)
    if failure == "runtime":
        def reject(*_args):
            raise RuntimeError("runtime identity mismatch")
        monkeypatch.setattr(installer, "_verify_pi_installation", reject)
    with pytest.raises(RuntimeError):
        installer._install_pi(pi="pi", package=new, dry_run=False, reinstall=True)
    assert settings.read_text() == original
    assert json.loads(installer.managed_runtime.binding_path("pi", agent).read_text())["package"] == str(old)
    assert not installer.managed_runtime.binding_path("pi-pending", agent).exists()


def test_pi_direct_skill_directory_has_one_removable_registration(installer, tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    _write_pi_settings(agent, [])
    skill = tmp_path / "skills/goal-plus"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("fixture")
    (agent / "settings.json").write_text(json.dumps({"skills": [str(skill)]}))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    entries = installer._pi_goal_plus_entries()
    assert len(entries) == 1
    assert entries[0]["removable"] is True


def test_pi_reinstall_removes_direct_resource_registration(installer, tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    _write_pi_settings(agent, [])
    settings = agent / "settings.json"
    extension = tmp_path / "goal-plus.ts"
    extension.write_text("// old")
    settings.write_text(json.dumps({"extensions": [str(extension), "unrelated.ts"]}))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    assert installer._preflight_pi_normal(allow_replacement=True)
    monkeypatch.setattr(installer, "_verify_pi_installation", lambda *args: None)
    monkeypatch.setattr(installer, "_run", lambda *args, **kwargs: None)
    installer._install_pi(pi="pi", package=tmp_path / "release/package", dry_run=False, reinstall=True)
    assert json.loads(settings.read_text())["extensions"] == ["unrelated.ts"]
    assert extension.exists()


@pytest.mark.parametrize("drift", [None, "duplicate", "disabled", "wrong_source", "obsolete_skill"])
def test_pi_verification_uses_enabled_host_resources(installer, tmp_path, monkeypatch, drift):
    agent = tmp_path / "agent"
    package = tmp_path / "goal-plus"
    _write_pi_settings(agent, [str(package)])
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    monkeypatch.setattr(installer, "_require_executable", lambda _: "node")
    monkeypatch.setattr(installer.managed_runtime, "verify", lambda _: {})
    resources = {
        "extensions": [{"path": str(package / "assets/pi/extensions/goal-plus.ts"), "enabled": drift != "disabled"}],
        "skills": [],
    }
    if drift == "duplicate":
        resources["extensions"].append({"path": str(ROOT / ".pi/extensions/goal-plus.ts"), "enabled": True})
    elif drift == "wrong_source":
        resources["extensions"][0]["path"] = str(ROOT / ".pi/extensions/goal-plus.ts")
    elif drift == "obsolete_skill":
        resources["skills"].append({"path": str(package / "assets/pi/skills/goal-plus/SKILL.md"), "enabled": True})
    monkeypatch.setattr(installer, "_run", lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(resources)))
    if drift:
        with pytest.raises(installer.InstallError, match="mismatch"):
            installer._verify_pi_installation("pi", package)
    else:
        installer._verify_pi_installation("pi", package)


def test_pi_shared_resource_registration_is_not_removed(installer, tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    resources = tmp_path / "extensions"
    resources.mkdir()
    (resources / "goal-plus.ts").write_text("// goal plus")
    (resources / "other.ts").write_text("// unrelated")
    _write_pi_settings(agent, [])
    settings = agent / "settings.json"
    original = json.dumps({"extensions": [str(resources)]})
    settings.write_text(original)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    with pytest.raises(installer.InstallError, match="conflicting"):
        installer._preflight_pi_normal(allow_replacement=True)
    with pytest.raises(installer.InstallError, match="shared"):
        installer._remove_pi_resource_registrations(installer._pi_goal_plus_entries(), dry_run=False)
    assert settings.read_text() == original
