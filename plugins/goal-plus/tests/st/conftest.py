from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests._st_tmp import new_st_log_dir, new_st_run_dir

from .helpers.codex_runner import CodexRunner, find_codex
from .hosts import (
    HOSTS,
    HostKind,
    link_host_assets,
    st_host_from_marker_names,
)


ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = Path(__file__).parent / "prompts"


@pytest.fixture()
def isolated_pi_config(monkeypatch):
    """Keep native model access while isolating package discovery and session writes."""
    source = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent"))).expanduser()
    with tempfile.TemporaryDirectory(prefix="goal-plus-pi-st-config-") as directory:
        target = Path(directory)
        for name in ("auth.json", "models.json", "models-store.json"):
            if (source / name).is_file():
                shutil.copyfile(source / name, target / name)
                (target / name).chmod(0o600)
        settings_path = source / "settings.json"
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        defaults = {key: settings[key] for key in (
            "defaultProvider", "defaultModel", "defaultThinkingLevel",
        ) if key in settings}
        (target / "settings.json").write_text(json.dumps(defaults))
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(target))
        yield target


@pytest.fixture
def installed_codex_config(tmp_path, monkeypatch):
    """Install current assets and trust their exact hooks in a disposable Codex home."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    from goal_plus.execution.session_rpc import SessionRpcProcess
    from goal_plus.ports.process import ProcessRegistration
    from goal_plus.ports.session import SessionProcessInput

    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    native_config = tomllib.loads((source / "config.toml").read_text()) if (source / "config.toml").is_file() else {}
    home = tmp_path / "installed-codex"
    home.mkdir()
    for name in ("auth.json",):
        if (source / name).is_file():
            shutil.copyfile(source / name, home / name)
            (home / name).chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("GOAL_PLUS_INSTALL_HOME", str(tmp_path / "managed-runtime"))
    monkeypatch.setenv("GOAL_PLUS_INSTALL_STATE_DIR", str(tmp_path / "install-state"))
    rpc = SessionRpcProcess(
        SessionProcessInput(("codex", "app-server", "--stdio"), tmp_path, dict(os.environ)),
        ProcessRegistration(register=lambda pid: "config-probe", finish=lambda token: None), lambda event: None,
    )

    def request(method, params):
        result = rpc.request({"method": method, "params": params}, timeout=40)
        if "error" in result:
            raise RuntimeError(f"isolated native configuration failed: {method}")
        return result["result"]

    try:
        request("initialize", {"clientInfo": {"name": "goal_plus_st", "version": "1"}, "capabilities": {"experimentalApi": True}})
        rpc.send({"method": "initialized", "params": {}}, timeout=5)
        edits = [{"keyPath": name, "value": native_config[name], "mergeStrategy": "replace"}
                 for name in ("model_provider", "model_providers", "model", "model_reasoning_effort") if name in native_config]
        if edits:
            request("config/batchWrite", {"edits": edits, "filePath": str(home / "config.toml")})
        rpc.close()
        installed = subprocess.run(
            [str(ROOT / "install.sh"), "--codex", "--yes"], cwd=ROOT,
            capture_output=True, text=True, timeout=300,
        )
        assert installed.returncode == 0, installed.stdout + installed.stderr
        bindings = list((tmp_path / "managed-runtime/bindings").glob("codex-*.json"))
        assert len(bindings) == 1
        package = Path(json.loads(bindings[0].read_text())["package"])
        rpc = SessionRpcProcess(
            SessionProcessInput(("codex", "app-server", "--stdio"), tmp_path, dict(os.environ)),
            ProcessRegistration(register=lambda pid: "config-probe", finish=lambda token: None), lambda event: None,
        )
        request("initialize", {"clientInfo": {"name": "goal_plus_st", "version": "1"}, "capabilities": {"experimentalApi": True}})
        rpc.send({"method": "initialized", "params": {}}, timeout=5)
        hooks = request("hooks/list", {"cwds": [str(tmp_path)]})["data"][0]
        assert not hooks["errors"]
        selected = [item for item in hooks["hooks"] if item.get("pluginId") == "goal-plus@goal-plus"]
        assert {item["eventName"] for item in selected} >= {"preToolUse", "stop", "sessionStart", "userPromptSubmit"}
        assert all(str(package / "scripts/run-runtime.sh") in item["command"] for item in selected)
        edits = [{"keyPath": "hooks.state", "value": {
            item["key"]: {"enabled": True, "trusted_hash": item["currentHash"]} for item in selected
        }, "mergeStrategy": "replace"}]
        request("config/batchWrite", {"edits": edits, "filePath": str(home / "config.toml")})
    finally:
        rpc.close()
    yield home


def _run(
    cmd: list[str],
    timeout: int = 20,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _codex_available() -> bool:
    return find_codex() is not None


def _codex_mcp_runtime_connected() -> tuple[bool, str]:
    if not _codex_available():
        return False, "codex binary not on PATH"
    proc = _run(["codex", "mcp", "list"], timeout=20, cwd=ROOT)
    if proc is None:
        return False, "codex mcp list timed out or failed to launch"
    if proc.returncode != 0:
        return False, f"codex mcp list exited {proc.returncode}: {proc.stderr.strip()[:200]}"
    if "goal-plus" not in proc.stdout:
        return False, "goal-plus MCP server not configured for Codex"
    return True, ""


def _mcp_server_binary_available() -> tuple[bool, str]:
    """Verify `goal-plus` (the MCP server entry point) is on PATH."""
    path = shutil.which("goal-plus")
    if path is None:
        return False, (
            "goal-plus binary not on PATH. "
            "Install with: pip install -e . (from project root)"
        )
    return True, ""


def _pi_console_scripts_available() -> list[tuple[bool, str]]:
    checks = []
    for command in ("goal-plus-pi-tool",):
        checks.append(
            (
                shutil.which(command) is not None,
                f"{command} binary not on PATH. Install with: pip install -e .",
            )
        )
    return checks


def _fixtures_present() -> tuple[bool, str]:
    """Verify the ST-local fixture specs and evaluators exist.

    ST fixtures live under tests/st/fixtures/<scenario>/ and are independent
    of examples/ and tests/fixtures/ — those are referenced by unit tests and
    example docs, not by ST.
    """
    missing = []
    for scenario in [
        "circle_packing",
    ]:
        for fname in ("spec.json", "evaluator.py", "initial_program.py", "config.yaml"):
            rel = f"tests/st/fixtures/{scenario}/{fname}"
            if not (ROOT / rel).exists():
                missing.append(rel)
    if missing:
        return False, f"missing ST fixture files under {ROOT}: {missing}"
    return True, ""


def _item_host(item: pytest.Item) -> HostKind:
    return st_host_from_marker_names({marker.name for marker in item.iter_markers()})


def _host_checks(agent_harness: HostKind) -> list[tuple[bool, str]]:
    checks: list[tuple[bool, str]] = []
    config = HOSTS[agent_harness]
    checks.append((shutil.which(config.binary) is not None, f"{config.binary} binary not on PATH"))
    if agent_harness == "pi":
        checks.extend(_pi_console_scripts_available())
    else:
        binary_ok, binary_msg = _mcp_server_binary_available()
        checks.append((binary_ok, binary_msg or "goal-plus on PATH"))

    if agent_harness == "codex" and _codex_available():
        mcp_ok, mcp_msg = _codex_mcp_runtime_connected()
        checks.append((mcp_ok, mcp_msg or "goal-plus MCP configured for Codex"))

    fixtures_ok, fixtures_msg = _fixtures_present()
    checks.append((fixtures_ok, fixtures_msg or "fixtures/specs present"))
    return checks


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Pre-flight checks for ST tests. Skip with a concrete reason if any check fails."""
    if not config.getoption("run_system_tests") or config.getoption("collectonly"):
        return

    # Pytest has already applied -m/-k. Only this adapter's surviving cases
    # may trigger preflight; TT and native Pi have their own checks.
    host_markers = {agent_harness.marker for agent_harness in HOSTS.values()}
    st_items = [
        item for item in items
        if host_markers.intersection(m.name for m in item.iter_markers())
    ]
    requested_hosts = {_item_host(item) for item in st_items}
    for agent_harness in requested_hosts:
        host_items = [item for item in st_items if _item_host(item) == agent_harness]
        if not host_items:
            continue
        checks = _host_checks(agent_harness)
        failed = [msg for ok, msg in checks if not ok]
        if failed:
            reason = (
                f"ST pre-flight check failed for {HOSTS[agent_harness].display_name}: "
                + " | ".join(failed)
            )
            for item in host_items:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture()
def st_project_root(request: pytest.FixtureRequest) -> Path:
    """Temporary project root for an ST run, under the ST scratch base.

    The scratch base lives outside the checkout (see tests/_st_tmp.py): a
    repo-local .tmp grew without bound and made the checkout look dirty to git.
    Each scenario gets its own subdirectory keyed by the test node id so
    parallel runs don't collide, and it is removed once the test finishes
    unless the run is kept for debugging.
    """
    project_root = new_st_run_dir(request, kind="st-runs")

    link_host_assets(project_root, ROOT)

    return project_root


@pytest.fixture()
def st_log_dir(request: pytest.FixtureRequest) -> Path:
    return new_st_log_dir(request.node.name)


@pytest.fixture()
def st_runner(
    st_project_root: Path,
    st_log_dir: Path,
):
    return CodexRunner(
        project_root=st_project_root,
        log_dir=st_log_dir,
        default_timeout=int(os.environ.get("ST_CODEX_TIMEOUT", "1800")),
    )


def load_prompt(scenario: str) -> str:
    """Load a prompt template and render {{PROJECT_ROOT}} with the absolute repo path."""
    path = PROMPTS_DIR / f"{scenario}.md"
    assert path.exists(), f"prompt file missing: {path}"
    text = path.read_text(encoding="utf-8")
    return text.replace("{{PROJECT_ROOT}}", str(ROOT))
