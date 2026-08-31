from __future__ import annotations

import importlib.util
import json
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
    assert str(ROOT / ".pi" / "extensions" / "goal-plus.ts") in launcher
    assert "--skill" in launcher
    assert str(ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md") in launcher
    assert 'export PATH="$goal_plus_venv/bin:$PATH"' in launcher
    assert "--check-pi-dev-runtime" in launcher
    subprocess.run(["bash", "-n", str(launcher_path)], check=True)


def test_pi_dev_rejects_user_registration(
    installer: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    _write_pi_settings(agent_dir, [str(ROOT)])
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    with pytest.raises(installer.InstallError, match="loads the checkout explicitly"):
        installer._preflight_pi_dev()


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


def test_pi_install_modes_are_mutually_exclusive(installer: ModuleType) -> None:
    with pytest.raises(SystemExit) as raised:
        installer.main(["--pi", "--pi-dev"])

    assert raised.value.code == 2
