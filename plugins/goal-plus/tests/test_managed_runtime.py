from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def managed(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("managed_install_test", Path(__file__).parents[1] / "scripts/managed_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("GOAL_PLUS_INSTALL_HOME", str(tmp_path / "managed"))
    return module


def source(tmp_path):
    root = tmp_path / "source"
    (root / "src/goal_plus").mkdir(parents=True)
    (root / "src/goal_plus/example.py").write_text("VALUE = 1\n")
    (root / "pyproject.toml").write_text('[project]\nname="goal-plus"\nversion="0.1.0"\n')
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_build_changes_with_code_or_dependency_lock_not_working_state(managed, tmp_path):
    root = source(tmp_path)
    first = managed.fingerprint(root)
    (root / ".gp").mkdir()
    (root / ".gp/goal.json").write_text("private state")
    assert managed.fingerprint(root) == first
    (root / "src/goal_plus/example.py").write_text("VALUE = 2\n")
    second = managed.fingerprint(root)
    assert second != first
    (root / "uv.lock").write_text("version = 1\n")
    assert managed.fingerprint(root) != second


def test_failed_preparation_leaves_existing_host_binding_and_release(managed, tmp_path, monkeypatch):
    root = source(tmp_path)
    old = managed.home() / "releases/old/package"
    managed.bind("codex", tmp_path / "codex", old)
    old.mkdir(parents=True)
    (old / "sentinel").write_text("old release")
    monkeypatch.setenv("GOAL_PLUS_UV", "/explicit/uv")
    original_run = subprocess.run
    def fail(argv, **kwargs):
        if argv[0] == "git":
            return original_run(argv, **kwargs)
        assert argv[0] == "/explicit/uv"
        assert "--managed-python" in argv and "--locked" in argv
        assert "PYTHONPATH" not in kwargs["env"]
        raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setenv("PYTHONPATH", "/wrong/source")
    monkeypatch.setattr(managed.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        managed.prepare(root)
    assert (old / "sentinel").read_text() == "old release"
    assert json.loads(managed.binding_path("codex", tmp_path / "codex").read_text())["package"] == str(old)
    assert not (managed.home() / "releases" / managed.fingerprint(root)).exists()


def test_cleanup_preserves_other_host_and_unmanaged_files(managed, tmp_path):
    def release(name):
        package = managed.home() / "releases" / name / "package"
        package.mkdir(parents=True)
        (package / ".goal-plus-runtime.json").write_text(json.dumps({"schema": managed.SCHEMA, "build": name, "package": str(package)}))
        return package
    managed.bind("pi", tmp_path / "pi", managed.home() / "releases/pi/package")
    pi = release("pi")
    codex = release("codex")
    managed.bind("codex", tmp_path / "codex", codex)
    managed.unbind("codex", tmp_path / "codex")
    managed.cleanup()
    assert pi.exists()
    assert not codex.exists()
    unknown = managed.home() / "releases/unknown"
    unknown.mkdir()
    with pytest.raises(RuntimeError, match="unrecognized release"):
        managed.cleanup()
    assert unknown.exists()


def test_dry_run_never_creates_installation(managed, tmp_path):
    managed.prepare(source(tmp_path), dry_run=True)
    assert not managed.home().exists()


@pytest.mark.parametrize("entrypoint", ["install.sh", "uninstall.sh"])
def test_shell_bootstrap_ignores_shell_python(entrypoint, tmp_path):
    import os
    import sys
    uv = tmp_path / "uv"
    uv.write_text(f"#!{sys.executable}\nimport json, os, sys\nprint(json.dumps({{'args':sys.argv[1:],'env':dict(os.environ)}}))\n")
    uv.chmod(0o755)
    result = subprocess.run(["/bin/bash", str(Path(__file__).parents[1] / entrypoint), "--help"],
        env={**os.environ, "GOAL_PLUS_UV": str(uv), "PYTHON": "/invalid/python", "PYTHONPATH": "/wrong",
             "PYTHONHOME": "/wrong", "CONDA_PREFIX": "/wrong", "UV_PYTHON": "/wrong"},
        text=True, capture_output=True, check=True)
    payload = json.loads(result.stdout)
    assert payload["args"][:7] == ["run", "--no-config", "--managed-python", "--python", "3.12", "--no-project", str(Path(__file__).parents[1] / "scripts" / entrypoint.replace('.sh', '.py'))]
    assert "PYTHONPATH" not in payload["env"]
    assert "PYTHONHOME" not in payload["env"]
    assert "UV_PYTHON" not in payload["env"]


def test_prepared_build_has_matching_manifest_receipt_and_interpreter(managed, tmp_path, monkeypatch):
    root = source(tmp_path)
    (root / "assets/codex/skills").mkdir(parents=True)
    (root / "assets/codex/plugin.json").write_text('{"version":"0.1.0"}')
    template = root / "assets/codex/marketplace.json"
    template.parent.mkdir(parents=True, exist_ok=True)
    (root / "assets/codex/skills/SKILL.md").write_text("fixture")
    template.write_bytes((Path(__file__).parents[1] / "assets/codex/marketplace.json").read_bytes())
    before = managed.fingerprint(root)
    marketplace = json.loads(template.read_text())
    marketplace["interface"]["displayName"] = "Updated Goal Plus"
    template.write_text(json.dumps(marketplace))
    assert managed.fingerprint(root) != before
    monkeypatch.setenv("GOAL_PLUS_UV", "/explicit/uv")
    calls = []
    original_run = subprocess.run
    def run(argv, **kwargs):
        if argv[0] == "git":
            return original_run(argv, **kwargs)
        calls.append(argv)
        if argv[0] == "/explicit/uv":
            package = Path(argv[-1])
            return subprocess.CompletedProcess(argv, 0)
        package = Path(argv[0]).parents[2] / "package"
        return subprocess.CompletedProcess(argv, 0, stdout=(package / "src/goal_plus/_installation.json").read_text())
    monkeypatch.setattr(managed.subprocess, "run", run)
    package = managed.prepare(root)
    receipt = managed.read_receipt(package)
    assert not (root / ".agents/plugins/marketplace.json").exists()
    assert json.loads((package / ".agents/plugins/marketplace.json").read_text()) == marketplace
    assert json.loads((package / ".codex-plugin/plugin.json").read_text())["version"] == "0.1.0+" + receipt["build"]
    assert (package / ".goal-plus-build").read_text().strip() == receipt["build"]
    assert (package / ".goal-plus-python").read_text().strip() == receipt["python"]
    assert managed.prepare(root) == package
    assert sum(call[0] == "/explicit/uv" for call in calls) == 1


def test_exact_build_entrypoint_rejects_mismatch(tmp_path):
    import sys
    root = Path(__file__).parents[1] / "src"
    code = f"import sys; sys.path.insert(0, {str(root)!r}); from goal_plus.installation import main; main()"
    result = subprocess.run([sys.executable, "-I", "-c", code, "--expected-build", "foreign",
                             "--module", "goal_plus.cli", "--version"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "build mismatch" in result.stderr


def test_prepared_host_commands_survive_removal_of_codex_cache(managed, tmp_path, monkeypatch):
    import shutil
    import sys
    root = source(tmp_path)
    (root / "assets/codex").mkdir(parents=True)
    (root / "assets/codex/hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "placeholder"}]}]}}))
    (root / "assets/codex/mcp.json").write_text(json.dumps({"mcpServers": {"goal-plus": {"args": ["placeholder", "--root", ".gp"]}}}))
    (root / "scripts").mkdir()
    (root / "scripts/run-runtime.sh").write_text('#!/bin/sh\nprintf "release-launcher\\n"\n')
    original_run = subprocess.run
    def run(argv, **kwargs):
        if argv[0] == "git":
            return original_run(argv, **kwargs)
        if argv[0] == sys.executable:
            return subprocess.CompletedProcess(argv, 0)
        package = Path(argv[0]).parents[2] / "package"
        return subprocess.CompletedProcess(argv, 0, stdout=(package / "src/goal_plus/_installation.json").read_text())
    monkeypatch.setenv("GOAL_PLUS_UV", sys.executable)
    with monkeypatch.context() as context:
        context.setattr(managed.subprocess, "run", run)
        package = managed.prepare(root)
    cache = tmp_path / "codex-cache"
    shutil.copytree(package, cache)
    command = json.loads((cache / "hooks/hooks.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    mcp_args = json.loads((cache / ".mcp.json").read_text())["mcpServers"]["goal-plus"]["args"]
    shutil.rmtree(cache)
    result = subprocess.run(["/bin/bash", "-c", command], text=True, capture_output=True, check=True)
    assert result.stdout == "release-launcher\n"
    assert Path(mcp_args[0]).is_file()
    assert str(cache) not in command


def test_build_launcher_forwards_help_to_target_cli(monkeypatch, capsys):
    import sys
    from goal_plus import installation
    monkeypatch.setattr(installation, "snapshot", lambda: {"build": "fixture"})
    monkeypatch.setattr(sys, "argv", ["installation", "--expected-build", "fixture", "--module", "goal_plus.cli", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        installation.main()
    assert exit_info.value.code == 0
    assert "--root" in capsys.readouterr().out


def test_managed_home_preserves_separately_owned_thinkthread_sdk(managed):
    sdk = managed.home() / "thinkthread-agent-posix/installed-sdk"
    sdk.mkdir(parents=True)
    sentinel = sdk / "ownership.json"
    sentinel.write_text('{"owner":"sdk-installer"}')
    with managed.locked():
        assert sentinel.read_text() == '{"owner":"sdk-installer"}'
    assert sentinel.is_file()
