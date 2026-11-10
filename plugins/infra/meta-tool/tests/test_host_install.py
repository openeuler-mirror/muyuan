"""Installer conflicts and package deployment; no package network or model calls."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location("host_install", Path(__file__).parents[1] / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def options(project, **changes):
    return argparse.Namespace(**dict(dict(project=project, pi=True, codex=False, config=None,
                                          trace_file=[], dry_run=False), **changes))


def test_dry_run_and_invalid_codex_options_do_not_install(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("dry run must not run subprocesses")
    monkeypatch.setattr(installer.subprocess, "run", forbidden)
    assert installer.install(options(tmp_path, codex=True, dry_run=True))["targets"] == ["pi", "codex"]
    assert not list(tmp_path.iterdir())
    with pytest.raises(SystemExit):
        installer.main(["--pi", "--trace-file", "a.jsonl", "--project", str(tmp_path)])
    assert not list(tmp_path.iterdir())


def test_pi_registration_preserves_settings_and_learning_policy(tmp_path):
    config = tmp_path / ".pi/meta-tool/config.json"
    installer.write_object(config, {"online": {"enabled": False}, "timeoutMs": 90000})
    settings = tmp_path / ".pi/settings.json"
    other = {"source": "npm:some-other-plugin", "extensions": ["*.ts"]}
    installer.write_object(settings, {"packages": [other], "defaultModel": "keep-model", "extensions": []})
    runtime = tmp_path / ".meta-tool"
    host, pi_config = installer.pi_configuration(tmp_path, runtime)
    assert host["packages"] == [other, str(runtime / "integrations/pi-meta-tool")]
    assert host["defaultModel"] == "keep-model"
    assert pi_config["online"] == {"enabled": False} and pi_config["timeoutMs"] == 90000
    installer.write_object(settings, host)
    installer.write_object(config, pi_config)
    assert installer.pi_configuration(tmp_path, runtime) == (host, pi_config)


@pytest.mark.parametrize("kind", ["foreign-runtime", "symlink", "invalid-settings", "custom-python", "duplicate-extension"])
def test_conflicts_fail_before_dependencies_or_registration(tmp_path, monkeypatch, kind):
    if kind == "foreign-runtime":
        (tmp_path / ".meta-tool").mkdir()
    elif kind == "symlink":
        (tmp_path / ".pi").symlink_to(tmp_path)
    elif kind == "invalid-settings":
        installer.write_object(tmp_path / ".pi/settings.json", {"packages": "invalid"})
    elif kind == "custom-python":
        installer.write_object(tmp_path / ".pi/meta-tool/config.json", {"pythonCommand": "my-python"})
    else:
        installer.write_object(tmp_path / ".pi/settings.json", {"extensions": ["/existing/meta-tool.ts"]})
    def forbidden(*a, **kw):
        pytest.fail("conflicts must be detected before installing dependencies")
    monkeypatch.setattr(installer.subprocess, "run", forbidden)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json") if p.is_file()}
    with pytest.raises(ValueError):
        installer.install(options(tmp_path))
    assert all(p.read_bytes() == data for p, data in before.items())


def test_deploy_copies_bridge_and_wrapper_and_keeps_previous_on_npm_failure(tmp_path, monkeypatch):
    target = tmp_path / "integrations/pi-meta-tool"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("previous version")
    def failed(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(installer.subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        installer.deploy_pi(tmp_path)
    assert (target / "old.txt").read_text() == "previous version"
    assert not list(tmp_path.glob(".pi-install-*"))
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: None)
    installer.deploy_pi(tmp_path)
    assert not (target / "old.txt").exists()
    assert (target / "bin/pi").stat().st_mode & 0o111
    assert (target / "python/bridge.py").is_file()
    assert (target / "python/discovery.py").is_file()
    assert (target / "src/bundled-read-batches.json").is_file()
    assert not (target / "tests").exists()


def test_pi_can_be_added_after_codex_without_losing_mcp_extra(tmp_path, monkeypatch):
    runtime = tmp_path / ".meta-tool"
    installer.write_object(runtime / "install.json", {"format": installer.FORMAT})
    installer.write_object(tmp_path / ".bate-codex/install.json", {"format": "bate-codex-install-v1"})
    calls = []
    monkeypatch.setattr(installer.shutil, "which", lambda name: name)
    monkeypatch.setattr(installer.subprocess, "check_output", lambda *a, **k: "v22.18.0\n")
    monkeypatch.setattr(installer.subprocess, "run", lambda command, **k: calls.append(command))
    monkeypatch.setattr(installer, "deploy_pi", lambda runtime: None)
    installer.install(options(tmp_path))
    assert any(c[-1] == str(installer.SOURCE) + "[codex]" for c in calls)
    assert json.loads((tmp_path / ".pi/settings.json").read_text())["packages"] == [
        str(runtime / "integrations/pi-meta-tool")]
