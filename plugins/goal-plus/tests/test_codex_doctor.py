from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from goal_plus import codex_doctor


def _make_project_assets(root: Path) -> dict[str, object]:
    hooks_payload = json.loads(
        (codex_doctor._package_root() / "assets/codex/hooks.json").read_text(
            encoding="utf-8"
        )
    )
    hooks_path = root / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(json.dumps(hooks_payload), encoding="utf-8")
    agent_root = root / ".codex" / "agents"
    agent_root.mkdir(parents=True)
    for name in (
        "search_candidate_agent.toml",
        "search_candidate_common_agent.toml",
    ):
        (agent_root / name).write_text(f'name = "{name[:-5]}"\n', encoding="utf-8")
    return hooks_payload


def _write_trusted_config(
    codex_home: Path,
    project_root: Path,
    hooks_payload: dict[str, object],
) -> None:
    hooks_path = project_root / ".codex" / "hooks.json"
    lines = [
        f'[projects.{json.dumps(str(project_root))}]',
        'trust_level = "trusted"',
        "",
        "[features]",
        "hooks = true",
    ]
    events = hooks_payload["hooks"]
    assert isinstance(events, dict)
    for event_name, event_label in codex_doctor.HOOK_EVENT_LABELS.items():
        groups = events[event_name]
        assert isinstance(groups, list)
        group = groups[0]
        assert isinstance(group, dict)
        handlers = group["hooks"]
        assert isinstance(handlers, list)
        handler = handlers[0]
        assert isinstance(handler, dict)
        current_hash = codex_doctor._canonical_hook_hash(event_name, group, handler)
        key = f"{hooks_path.resolve()}:{event_label}:0:0"
        lines.extend(
            (
                "",
                f'[hooks.state.{json.dumps(key)}]',
                f'trusted_hash = "{current_hash}"',
            )
        )
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_canonical_hook_hash_matches_codex_0148_identity() -> None:
    payload = json.loads(
        (codex_doctor._package_root() / "assets/codex/hooks.json").read_text(
            encoding="utf-8"
        )
    )
    group = payload["hooks"]["PreToolUse"][0]
    handler = group["hooks"][0]

    assert codex_doctor._canonical_hook_hash("PreToolUse", group, handler) == (
        "sha256:3c9d43cf29a492b054f997a86b4955a82f15b73468eea3e3dc8cde9affafef66"
    )


def test_parse_plugin_installation_with_enabled_local_plugin(tmp_path: Path) -> None:
    output = (
        "PLUGIN STATUS VERSION PATH\n"
        f"goal-plus@muyuan installed, enabled 0.1.0 {tmp_path}\n"
    )

    installation = codex_doctor._parse_plugin_installation(output)

    assert installation is not None
    assert installation.plugin_id == "goal-plus@muyuan"
    assert installation.status == "installed, enabled"
    assert installation.path == tmp_path.resolve()


def test_enabled_plugin_uses_native_roles_without_skills(tmp_path: Path) -> None:
    installation = codex_doctor.PluginInstallation(
        plugin_id="goal-plus@muyuan",
        status="installed, enabled",
        path=tmp_path,
    )

    roles = codex_doctor._check_agent_roles(
        installation=installation,
        project_root=tmp_path / "unrelated-project",
    )
    assert roles.status == "pass"
    assert roles.details["search_worker"] == "default"
    assert roles.details["final_checker"] == "native-session-read-only"
    assert roles.details["custom_agents_packaged"] is False


def test_project_hooks_require_current_trusted_hashes(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    codex_home = tmp_path / "codex-home"
    payload = _make_project_assets(project_root)
    _write_trusted_config(codex_home, project_root, payload)

    passing = codex_doctor._check_hooks(
        codex_home=codex_home,
        project_root=project_root,
        installation=None,
    )
    assert passing.status == "pass"

    hook_path = project_root / ".codex" / "hooks.json"
    modified = json.loads(hook_path.read_text(encoding="utf-8"))
    modified["hooks"]["Stop"][0]["hooks"][0]["statusMessage"] = "changed"
    hook_path.write_text(json.dumps(modified), encoding="utf-8")

    failing = codex_doctor._check_hooks(
        codex_home=codex_home,
        project_root=project_root,
        installation=None,
    )
    assert failing.status == "fail"
    assert any(
        failure["reason"] == "modified or stale trust hash"
        for failure in failing.details["failures"]
    )


def test_project_hooks_require_interrupt_for_native_pause(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    codex_home = tmp_path / "codex-home"
    payload = _make_project_assets(project_root)
    _write_trusted_config(codex_home, project_root, payload)
    del payload["hooks"]["Interrupt"]
    (project_root / ".codex" / "hooks.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    result = codex_doctor._check_hooks(
        codex_home=codex_home, project_root=project_root, installation=None,
    )
    assert result.status == "fail"
    assert {"event": "Interrupt", "reason": "missing event"} in result.details["failures"]


@pytest.mark.parametrize("codex_version", ["0.148.0", "0.151.0", "dev-build"])
def test_collect_report_accepts_complete_project_local_setup(
    tmp_path: Path,
    monkeypatch,
    codex_version: str,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    codex_home = tmp_path / "codex-home"
    payload = _make_project_assets(project_root)
    _write_trusted_config(codex_home, project_root, payload)
    # Hermetic XDG roots: the optional ThinkThread check must not observe the
    # developer machine's real profiles or SDK receipts.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    def fake_which(name: str) -> str:
        return f"/mock/bin/{name}"

    def fake_run(
        argv: list[str],
        *,
        codex_home: Path,
        timeout: float = 20.0,
    ) -> subprocess.CompletedProcess[str]:
        del codex_home, timeout
        if argv[-1] == "--version" and argv[0].endswith("codex"):
            stdout = f"codex-cli {codex_version}\n"
        elif argv[-1] == "--version" and argv[0].endswith("goal-plus"):
            stdout = "goal-plus 0.1.0\n"
        elif argv[-2:] == ["plugin", "list"]:
            stdout = "PLUGIN STATUS VERSION PATH\n"
        elif argv[-2:] == ["mcp", "list"]:
            stdout = "goal-plus goal-plus --root .gp enabled Unsupported\n"
        else:  # pragma: no cover - catches accidental new subprocess probes
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(codex_doctor, "_run", fake_run)
    monkeypatch.setattr(
        "goal_plus.agent_hosts.CodexModelCatalog.list_available_models",
        lambda self, query=None: [{"model": "gpt-5.6-luna"}],
    )

    report = codex_doctor.collect_report(
        project_root=project_root,
        codex_home=codex_home,
    )

    failures = [
        (check.name, check.summary, check.details)
        for check in report.checks
        if check.status == "fail"
    ]
    assert not failures
    assert report.ok
    assert {check.name: check.status for check in report.checks} == {
        "codex.version": "pass",
        "goal_plus.executable": "warn",
        "goal_plus.package_assets": "pass",
        "codex.plugin": "warn",
        "codex.agent_roles": "pass",
        "codex.mcp": "pass",
        "codex.hooks": "pass",
        "codex.models": "pass",
        "thinkthread.installation": "info",
        "codex.session_reload": "info",
    }


def test_plugin_templates_declare_supported_mcp_entrypoint() -> None:
    root = codex_doctor._package_root()
    manifest = json.loads(
        (root / "assets/codex/plugin.json").read_text(encoding="utf-8")
    )
    mcp = json.loads((root / "assets/codex/mcp.json").read_text(encoding="utf-8"))

    assert "hooks" not in manifest
    assert manifest["mcpServers"] == "./.mcp.json"
    assert mcp["mcpServers"]["goal-plus"]["command"] == "bash"
    assert mcp["mcpServers"]["goal-plus"]["args"] == ["${PLUGIN_ROOT}/scripts/run-runtime.sh", "--root", ".gp"]
    assert "PI_CODING_AGENT_DIR" in mcp["mcpServers"]["goal-plus"]["env_vars"]
    assert (
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL"
        not in mcp["mcpServers"]["goal-plus"]["env_vars"]
    )


def test_enabled_plugin_rejects_duplicate_ancestor_goal_plus_hooks(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "checkout" / "examples" / "target"
    project_root.mkdir(parents=True)
    codex_home = tmp_path / "codex-home"
    plugin_root = tmp_path / "installed-plugin"
    plugin_hooks = plugin_root / "hooks/hooks.json"
    plugin_hooks.parent.mkdir(parents=True)
    payload = json.loads(
        (codex_doctor._package_root() / "assets/codex/hooks.json").read_text(
            encoding="utf-8"
        )
    )
    plugin_hooks.write_text(json.dumps(payload), encoding="utf-8")
    duplicate = project_root.parents[1] / ".codex" / "hooks.json"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text(json.dumps(payload), encoding="utf-8")
    installation = codex_doctor.PluginInstallation(
        plugin_id="goal-plus@goal-plus",
        status="installed, enabled",
        path=plugin_root,
    )

    result = codex_doctor._check_hooks(
        codex_home=codex_home,
        project_root=project_root,
        installation=installation,
    )

    assert result.status == "fail"
    assert "both declare Goal Plus hooks" in result.summary
    assert result.details["project_hooks"] == [str(duplicate.resolve())]


def test_marketplace_template_points_at_release_without_source_discovery() -> None:
    root = codex_doctor._package_root()
    assert not (root / ".agents/plugins/marketplace.json").exists()
    marketplace = json.loads(
        (root / "assets/codex/marketplace.json").read_text(
            encoding="utf-8"
        )
    )

    assert marketplace["name"] == "goal-plus"
    assert marketplace["plugins"] == [
        {
            "name": "goal-plus",
            "source": {"source": "local", "path": "."},
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
            "category": "Developer Tools",
        }
    ]


@pytest.mark.parametrize("actual_build", ["same-build", "wrong-build"])
def test_doctor_checks_managed_build_instead_of_path(tmp_path, monkeypatch, actual_build):
    receipt = {"schema": "goal-plus.managed-runtime.v1", "build": "same-build",
               "python": "/managed/bin/python", "package": str(tmp_path)}
    (tmp_path / ".goal-plus-runtime.json").write_text(json.dumps(receipt))
    (tmp_path / ".codex-plugin").mkdir()
    (tmp_path / ".codex-plugin/plugin.json").write_text(json.dumps({"version": "0.1.0+same-build"}))
    def run(argv, **kwargs):
        assert argv == ["/managed/bin/python", "-I", "-m", "goal_plus.installation"]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({**receipt, "build": actual_build}))
    monkeypatch.setattr(codex_doctor, "_run", run)
    result = codex_doctor._check_goal_plus_cli(tmp_path, tmp_path)
    assert result.status == ("pass" if actual_build == "same-build" else "fail")
