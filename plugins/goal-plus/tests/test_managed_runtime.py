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


def test_provider_environment_names_are_part_of_immutable_build(managed, tmp_path, monkeypatch):
    root = source(tmp_path)
    first = managed.fingerprint(root, codex_env_vars=("MODEL_TOKEN", "HEADER_TOKEN"))
    assert managed.fingerprint(root, codex_env_vars=("HEADER_TOKEN", "MODEL_TOKEN")) == first
    monkeypatch.setenv("MODEL_TOKEN", "local-secret-value")
    assert managed.fingerprint(root, codex_env_vars=("MODEL_TOKEN", "HEADER_TOKEN")) == first
    assert managed.fingerprint(root, codex_env_vars=("ANOTHER_TOKEN",)) != first


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
    (root / "assets/codex").mkdir(parents=True)
    (root / "assets/codex/plugin.json").write_text('{"version":"0.1.0"}')
    template = root / "assets/codex/marketplace.json"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_bytes((Path(__file__).parents[1] / "assets/codex/marketplace.json").read_bytes())
    (root / "assets/codex/mcp.json").write_text(json.dumps({
        "mcpServers": {"goal-plus": {"args": ["launcher"], "env_vars": ["CODEX_HOME"]}},
    }))
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
    monkeypatch.setenv("MODEL_TOKEN", "local-secret-value")
    package = managed.prepare(root, codex_env_vars=("MODEL_TOKEN",))
    assert not list(package.parent.rglob("SKILL.md"))
    assert not (package.parent / "skills").exists()
    receipt = managed.read_receipt(package)
    assert receipt["capabilities"] == list(managed.RUNTIME_CAPABILITIES)
    assert not (root / ".agents/plugins/marketplace.json").exists()
    assert json.loads((package / ".agents/plugins/marketplace.json").read_text()) == marketplace
    assert json.loads((package / ".codex-plugin/plugin.json").read_text())["version"] == "0.1.0+" + receipt["build"]
    assert (package / ".goal-plus-build").read_text().strip() == receipt["build"]
    assert (package / ".goal-plus-python").read_text().strip() == receipt["python"]
    mcp = (package / ".mcp.json").read_text()
    assert json.loads(mcp)["mcpServers"]["goal-plus"]["env_vars"] == ["CODEX_HOME", "MODEL_TOKEN"]
    assert "local-secret-value" not in mcp
    assert managed.prepare(root, codex_env_vars=("MODEL_TOKEN",)) == package
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


def test_installed_info_command_selects_host_binding_and_survives_uninstall(managed, tmp_path, monkeypatch):
    import os
    import sys

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex config"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi config"))
    for host, config in (("codex", "CODEX_HOME"), ("pi", "PI_CODING_AGENT_DIR")):
        package = managed.home() / "releases" / host / "package"
        managed.bind(host, Path(os.environ[config]), package)
        package.mkdir(parents=True)
        python = package.parent / "python"
        python.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps({{'host':{host!r}, 'args':sys.argv[1:]}}))\n")
        python.chmod(0o755)
        (package / ".goal-plus-runtime.json").write_text(json.dumps({
            "schema": managed.SCHEMA, "build": host, "python": str(python), "package": str(package),
        }))
    launcher = managed.install_info_command()
    for host in ("codex", "pi"):
        result = subprocess.run([str(launcher), host, "--json"], cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        info = json.loads(result.stdout)
        assert info == {"host": host, "args": ["-I", "-m", "goal_plus.installation", "--expected-build",
                                              host, "--module", "goal_plus.cli", "--runtime-info", host,
                                              "--runtime-provider", "direct", "--json"]}
    managed.unbind("codex", Path(os.environ["CODEX_HOME"]))
    managed.cleanup()
    result = subprocess.run([str(launcher), "codex"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert "install.sh --codex" in result.stderr
    assert "Traceback" not in result.stderr
    assert subprocess.run([str(launcher), "pi"], capture_output=True).returncode == 0
    assert not (tmp_path / ".gp").exists()


def test_info_command_dry_run_does_not_create_state(managed):
    managed.install_info_command(dry_run=True)
    assert not managed.home().exists()


def test_info_refresh_does_not_follow_fixed_temporary_symlinks(managed, tmp_path):
    launcher = managed.install_info_command()
    victim = tmp_path / "outside.txt"
    victim.write_text("preserve outside")
    for name in ("runtime-info.tmp", "goal-plus-info.tmp"):
        (launcher.parent / name).symlink_to(victim)
    managed.install_info_command()
    assert victim.read_text() == "preserve outside"


@pytest.mark.parametrize("target", ["directory", "launcher", "query", "foreign"])
def test_info_refresh_rejects_unowned_or_linked_targets(managed, tmp_path, target):
    launcher = managed.install_info_command()
    victim = tmp_path / "outside.txt"
    victim.write_text("preserve outside")
    if target == "directory":
        original = launcher.parent
        moved = original.with_name("moved")
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
    else:
        path = launcher if target != "query" else launcher.with_name("runtime-info.py")
        path.unlink()
        if target == "foreign":
            path.write_text("user owned command")
        else:
            path.symlink_to(victim)
    with pytest.raises(RuntimeError, match="Refusing"):
        managed.install_info_command()
    assert victim.read_text() == "preserve outside"
    if target == "foreign":
        assert launcher.read_text() == "user owned command"


def test_info_refresh_failure_restores_previous_files(managed, monkeypatch):
    launcher = managed.install_info_command()
    query = launcher.with_name("runtime-info.py")
    original = {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in (launcher, query)}
    source = Path(managed.__file__).with_name("runtime-info.py")
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: read_bytes(path) + b"\n# next build\n" if path == source else read_bytes(path))
    replace = Path.replace
    failed = False
    def fail_once(path, destination):
        nonlocal failed
        if destination == launcher and not failed:
            failed = True
            raise OSError("fixture publication failure")
        return replace(path, destination)
    monkeypatch.setattr(Path, "replace", fail_once)
    with pytest.raises(OSError, match="publication failure"):
        managed.install_info_command()
    assert {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in original} == original


def test_info_refresh_updates_owned_code_and_adopts_exact_preview(managed, monkeypatch):
    import shlex
    import sys

    # The preview installed earlier had no ownership marker. Only exact known
    # bytes may be adopted; arbitrary unmarked commands are covered above.
    with managed.locked():
        directory = managed.home() / "bin"
        directory.mkdir()
    query = directory / "runtime-info.py"
    source = Path(managed.__file__).with_name("runtime-info.py")
    query.write_bytes(source.read_bytes())
    launcher = directory / "goal-plus-info"
    launcher.write_text("#!/bin/bash\nexec " + shlex.quote(sys.executable)
                        + " -I " + shlex.quote(str(query)) + ' "$@"\n')
    managed.install_info_command()
    assert query.read_bytes().startswith(managed.INFO_COMMAND_MARKER)
    assert managed.INFO_COMMAND_MARKER in launcher.read_bytes().splitlines(keepends=True)[:2]
    before = (query.read_bytes(), launcher.read_bytes())
    managed.install_info_command()
    assert (query.read_bytes(), launcher.read_bytes()) == before
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: read_bytes(path) + b"\n# updated selector\n" if path == source else read_bytes(path))
    managed.install_info_command()
    assert query.read_bytes().endswith(b"# updated selector\n")
    result = subprocess.run([str(launcher), "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--thinkthread" in result.stdout


def test_first_info_install_failure_removes_partial_files(managed, monkeypatch):
    launcher = managed.home() / "bin/goal-plus-info"
    replace = Path.replace
    def fail(path, destination):
        if destination == launcher:
            raise OSError("fixture publication failure")
        return replace(path, destination)
    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="publication failure"):
        managed.install_info_command()
    assert not launcher.exists()
    assert not launcher.with_name("runtime-info.py").exists()
    assert not list(launcher.parent.glob(".*"))


def test_info_refresh_reports_rollback_failure(managed, monkeypatch):
    launcher = managed.install_info_command()
    original_replace = managed._replace_info_file
    updated = False
    def fail(path, content, mode):
        nonlocal updated
        if updated:
            raise OSError("fixture filesystem unavailable")
        original_replace(path, content, mode)
        updated = True
    monkeypatch.setattr(managed, "_replace_info_file", fail)
    with pytest.raises(RuntimeError, match="refresh failed and rollback failed"):
        managed.install_info_command()


@pytest.mark.parametrize("host", ["codex", "pi"])
def test_info_command_selects_thinkthread_profile_not_direct_installation(managed, tmp_path, monkeypatch, host):
    import sys

    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    direct_config = tmp_path / "direct-config"
    monkeypatch.setenv("CODEX_HOME" if host == "codex" else "PI_CODING_AGENT_DIR", str(direct_config))
    profile = config_home / "thinkthread/profiles" / f"goal-plus-{host}.toml"
    for provider, binding_host, config in [("direct", host, direct_config), ("thinkthread", "thinkthread", profile)]:
        build = f"{host}-{provider}"
        package = managed.home() / "releases" / build / "package"
        managed.bind(binding_host, config, package)
        package.mkdir(parents=True)
        python = package.parent / "python"
        python.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps({{'build':{build!r}, 'args':sys.argv[1:]}}))\n")
        python.chmod(0o755)
        (package / ".goal-plus-runtime.json").write_text(json.dumps({
            "schema": managed.SCHEMA, "build": build, "python": str(python), "package": str(package),
        }))
    launcher = managed.install_info_command()
    result = subprocess.run([str(launcher), host, "--thinkthread", "--json"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["build"] == f"{host}-thinkthread"
    assert value["args"][-5:] == ["--runtime-info", host, "--runtime-provider", "thinkthread", "--json"]
    assert "--root" not in value["args"]
    managed.unbind("thinkthread", profile)
    result = subprocess.run([str(launcher), host, "--thinkthread"], capture_output=True, text=True)
    assert result.returncode == 1
    assert f"install.sh --{host} --thinkthread" in result.stderr
    assert not result.stdout
