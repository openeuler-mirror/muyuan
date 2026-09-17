"""Read-only Codex installation preflight for Goal Plus.

Run with ``python -m goal_plus.codex_doctor``.  The doctor deliberately does
not edit Codex configuration or trust hooks on the user's behalf.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from goal_plus.paths import host_assets_root

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


HOOK_EVENT_LABELS = {
    "UserPromptSubmit": "user_prompt_submit",
    "SessionStart": "session_start",
    "Interrupt": "interrupt",
    "PreToolUse": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "Stop": "stop",
    "SubagentStop": "subagent_stop",
}
Status = Literal["pass", "warn", "fail", "info"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    summary: str
    details: dict[str, Any]


@dataclass(frozen=True)
class PluginInstallation:
    plugin_id: str
    status: str
    path: Path | None


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]
    project_root: str
    codex_home: str
    source_root: str

    @property
    def ok(self) -> bool:
        return not any(check.status == "fail" for check in self.checks)

    @property
    def warning_count(self) -> int:
        return sum(check.status == "warn" for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": (
                "ready_with_warnings"
                if self.ok and self.warning_count
                else "ready"
                if self.ok
                else "not_ready"
            ),
            "project_root": self.project_root,
            "codex_home": self.codex_home,
            "source_root": self.source_root,
            "checks": [asdict(check) for check in self.checks],
            "next_action": (
                "After install/update, start a new Codex thread, review and trust Goal Plus "
                "in /hooks, then start a fresh thread so every plugin surface is active."
            ),
        }


def _package_root() -> Path:
    return host_assets_root()


def _run(
    argv: list[str],
    *,
    codex_home: Path,
    timeout: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _check_codex_version(codex_home: Path) -> CheckResult:
    executable = shutil.which("codex")
    if executable is None:
        return CheckResult(
            "codex.version",
            "fail",
            "The `codex` executable is not on PATH.",
            {},
        )
    try:
        result = _run([executable, "--version"], codex_home=codex_home)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            "codex.version",
            "fail",
            f"Could not run Codex: {exc}",
            {"executable": executable},
        )
    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        return CheckResult(
            "codex.version",
            "fail",
            "Could not query the Codex version.",
            {"executable": executable, "output": output},
        )
    return CheckResult(
        "codex.version",
        "pass",
        "Codex executable is available.",
        {"executable": executable, "version_output": output},
    )


def _check_goal_plus_cli(codex_home: Path, source_root: Path) -> CheckResult:
    receipt_path = source_root / ".goal-plus-runtime.json"
    if not receipt_path.is_file():
        return CheckResult(
            "goal_plus.executable", "warn", "No managed runtime receipt was found for this source.",
            {"source": str(source_root), "fix": "Run ./install.sh --codex from the source checkout."},
        )
    try:
        receipt = json.loads(receipt_path.read_text())
        result = _run([receipt["python"], "-I", "-m", "goal_plus.installation"], codex_home=codex_home)
        actual = json.loads(result.stdout)
        manifest = json.loads((source_root / ".codex-plugin/plugin.json").read_text())
        matches = (
            result.returncode == 0
            and receipt.get("schema") == "goal-plus.managed-runtime.v1"
            and all(actual.get(key) == receipt.get(key) for key in ("build", "python", "package"))
            and bool(receipt.get("build"))
            and manifest["version"].endswith("+" + receipt["build"])
        )
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        return CheckResult("goal_plus.executable", "fail", f"Could not verify managed runtime: {exc}", {"receipt": str(receipt_path)})
    return CheckResult(
        "goal_plus.executable", "pass" if matches else "fail",
        "The host package and managed Python have the same build." if matches else "Managed runtime build mismatch.",
        {"receipt": str(receipt_path), "expected": receipt, "actual": actual},
    )


def _parse_plugin_installation(output: str) -> PluginInstallation | None:
    statuses = ("installed, enabled", "installed, disabled", "not installed")
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("goal-plus@"):
            continue
        plugin_id, _, remainder = line.partition(" ")
        remainder = remainder.strip()
        status = next((item for item in statuses if remainder.startswith(item)), "unknown")
        tail = remainder[len(status) :].strip() if status != "unknown" else remainder
        path: Path | None = None
        if status.startswith("installed") and tail:
            version_and_path = tail.split(maxsplit=1)
            if len(version_and_path) == 2:
                candidate = Path(version_and_path[1]).expanduser()
                if candidate.is_dir():
                    path = candidate.resolve()
        return PluginInstallation(plugin_id=plugin_id, status=status, path=path)
    return None


def _check_plugin(
    codex_home: Path,
    *,
    project_root: Path,
) -> tuple[CheckResult, PluginInstallation | None]:
    executable = shutil.which("codex")
    if executable is None:
        return (
            CheckResult(
                "codex.plugin",
                "fail",
                "Cannot inspect plugins because Codex is unavailable.",
                {},
            ),
            None,
        )
    try:
        result = _run([executable, "plugin", "list"], codex_home=codex_home)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (
            CheckResult(
                "codex.plugin",
                "fail",
                f"Could not list Codex plugins: {exc}",
                {},
            ),
            None,
        )
    installation = _parse_plugin_installation(result.stdout)
    if installation is not None and installation.status == "installed, enabled":
        return (
            CheckResult(
                "codex.plugin",
                "pass",
                f"{installation.plugin_id} is installed and enabled.",
                {"plugin_path": str(installation.path) if installation.path else None},
            ),
            installation,
        )
    project_hooks = project_root / ".codex" / "hooks.json"
    if project_hooks.is_file():
        return (
            CheckResult(
                "codex.plugin",
                "warn",
                "Legacy project-local hooks detected; use install.sh --codex to install the supported plugin.",
                {
                    "project_hooks": str(project_hooks),
                    "plugin_status": installation.status if installation else "not listed",
                },
            ),
            installation,
        )
    return (
        CheckResult(
            "codex.plugin",
            "fail",
            "Goal Plus is neither an enabled plugin nor available as project-local assets.",
            {"plugin_status": installation.status if installation else "not listed"},
        ),
        installation,
    )


def _check_agent_roles(
    *,
    installation: PluginInstallation | None,
    project_root: Path,
) -> CheckResult:
    if installation is not None and installation.status == "installed, enabled":
        return CheckResult(
            "codex.agent_roles",
            "pass",
            "The installable plugin uses self-contained built-in roles.",
            {
                "search_worker": "default",
                "final_checker": "native-session-read-only",
                "custom_agents_packaged": False,
            },
        )
    agent_root = project_root / ".codex" / "agents"
    required = (
        "search_candidate_agent.toml",
        "search_candidate_common_agent.toml",
    )
    missing = [name for name in required if not (agent_root / name).is_file()]
    if missing:
        return CheckResult(
            "codex.agent_roles",
            "fail",
            "Project-local Codex agent roles are incomplete.",
            {"agent_root": str(agent_root), "missing": missing},
        )
    return CheckResult(
        "codex.agent_roles",
        "pass",
        "Project-local role templates are present; runtime launches use built-in roles.",
        {"agent_root": str(agent_root), "roles": list(required)},
    )


def _check_mcp(codex_home: Path) -> CheckResult:
    executable = shutil.which("codex")
    if executable is None:
        return CheckResult("codex.mcp", "fail", "Codex is unavailable.", {})
    try:
        result = _run([executable, "mcp", "list"], codex_home=codex_home)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            "codex.mcp", "fail", f"Could not list Codex MCP servers: {exc}", {}
        )
    matching_line = next(
        (line.strip() for line in result.stdout.splitlines() if line.strip().startswith("goal-plus ")),
        None,
    )
    if matching_line is None:
        return CheckResult(
            "codex.mcp",
            "fail",
            "The Goal Plus MCP server is not listed.",
            {"fix": "Install/enable the plugin or configure [mcp_servers.goal-plus]."},
        )
    enabled = bool(re.search(r"\benabled\b", matching_line)) and not bool(
        re.search(r"\bdisabled\b", matching_line)
    )
    return CheckResult(
        "codex.mcp",
        "pass" if enabled else "fail",
        "The Goal Plus MCP server is enabled."
        if enabled
        else "The Goal Plus MCP server is listed but disabled.",
        {"entry": matching_line},
    )


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        loaded = tomllib.load(handle)
    return loaded if isinstance(loaded, dict) else {}


def _canonical_hook_hash(event_name: str, group: dict[str, Any], handler: dict[str, Any]) -> str:
    timeout = max(int(handler.get("timeout", 600)), 1)
    normalized_handler: dict[str, Any] = {
        "type": "command",
        "command": str(handler["command"]),
        "timeout": timeout,
        "async": bool(handler.get("async", False)),
    }
    status_message = handler.get("statusMessage")
    if status_message is not None:
        normalized_handler["statusMessage"] = str(status_message)
    additional_context_limit = handler.get("additionalContextLimit")
    if additional_context_limit is not None and int(additional_context_limit) != 2500:
        normalized_handler["additionalContextLimit"] = int(additional_context_limit)
    identity: dict[str, Any] = {
        "event_name": HOOK_EVENT_LABELS[event_name],
        "hooks": [normalized_handler],
    }
    matcher = group.get("matcher")
    if matcher is not None:
        identity["matcher"] = str(matcher)
    serialized = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(serialized).hexdigest()


def _project_is_trusted(config: dict[str, Any], project_root: Path) -> tuple[bool, str | None]:
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        return False, None
    matches: list[Path] = []
    for raw_path, value in projects.items():
        if not isinstance(raw_path, str) or not isinstance(value, dict):
            continue
        try:
            configured = Path(raw_path).expanduser().resolve()
        except OSError:
            continue
        if (project_root == configured or configured in project_root.parents) and value.get(
            "trust_level"
        ) == "trusted":
            matches.append(configured)
    if not matches:
        return False, None
    nearest = max(matches, key=lambda path: len(path.parts))
    return True, str(nearest)


def _hook_source(
    *,
    installation: PluginInstallation | None,
    project_root: Path,
) -> tuple[Path | None, str | None, Literal["plugin", "project"]]:
    if installation is not None and installation.status == "installed, enabled" and installation.path:
        plugin_hooks = installation.path / "hooks" / "hooks.json"
        if plugin_hooks.is_file():
            return plugin_hooks, f"{installation.plugin_id}:hooks/hooks.json", "plugin"
    project_hooks = project_root / ".codex" / "hooks.json"
    if project_hooks.is_file():
        return project_hooks, str(project_hooks.resolve()), "project"
    return None, None, "project"


def _ancestor_goal_plus_hooks(project_root: Path, codex_home: Path) -> list[Path]:
    user_hook = (codex_home / "hooks.json").resolve()
    matches: list[Path] = []
    for ancestor in (project_root, *project_root.parents):
        candidate = ancestor / ".codex" / "hooks.json"
        if not candidate.is_file() or candidate.resolve() == user_hook:
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        events = payload.get("hooks", {})
        if not isinstance(events, dict):
            continue
        handlers = (
            handler
            for groups in events.values()
            if isinstance(groups, list)
            for group in groups
            if isinstance(group, dict)
            for handler in group.get("hooks", [])
            if isinstance(handler, dict)
        )
        if any(
            isinstance(handler.get("command"), str) and (
                handler["command"] == "goal-plus --goal-plus-host-hook"
                or "/scripts/run-runtime.sh" in handler["command"]
            )
            for handler in handlers
        ):
            matches.append(candidate.resolve())
    return matches


def _check_hooks(
    *,
    codex_home: Path,
    project_root: Path,
    installation: PluginInstallation | None,
) -> CheckResult:
    config_path = codex_home / "config.toml"
    hooks_path, key_source, source_kind = _hook_source(
        installation=installation,
        project_root=project_root,
    )
    if hooks_path is None or key_source is None:
        return CheckResult(
            "codex.hooks",
            "fail",
            "No loadable Goal Plus hooks file was found.",
            {"project_root": str(project_root)},
        )
    if source_kind == "plugin":
        duplicate_sources = _ancestor_goal_plus_hooks(project_root, codex_home)
        if duplicate_sources:
            return CheckResult(
                "codex.hooks",
                "fail",
                "The enabled plugin and project-local files both declare Goal Plus hooks.",
                {
                    "plugin_hooks": str(hooks_path),
                    "project_hooks": [str(path) for path in duplicate_sources],
                    "fix": (
                        "Remove the project-local .codex/hooks.json or disable the "
                        "installed plugin; use only one Goal Plus hook source."
                    ),
                },
            )
    if not config_path.is_file():
        return CheckResult(
            "codex.hooks",
            "fail",
            "Codex config.toml is missing, so hook trust cannot be verified.",
            {"config_path": str(config_path), "hooks_path": str(hooks_path)},
        )
    try:
        hooks_payload = json.loads(hooks_path.read_text(encoding="utf-8"))
        config = _load_toml(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return CheckResult(
            "codex.hooks",
            "fail",
            f"Could not parse hook/config state: {exc}",
            {"config_path": str(config_path), "hooks_path": str(hooks_path)},
        )
    if config.get("features", {}).get("hooks") is False:
        return CheckResult(
            "codex.hooks",
            "fail",
            "Codex hooks are explicitly disabled.",
            {"config_path": str(config_path)},
        )
    if source_kind == "project":
        trusted_project, trusted_ancestor = _project_is_trusted(config, project_root)
        if not trusted_project:
            return CheckResult(
                "codex.hooks",
                "fail",
                "The project-local hook source is not in a trusted Codex project.",
                {"project_root": str(project_root), "hooks_path": str(hooks_path)},
            )
    else:
        trusted_ancestor = None

    hook_states = config.get("hooks", {}).get("state", {})
    if not isinstance(hook_states, dict):
        hook_states = {}
    events = hooks_payload.get("hooks", {})
    failures: list[dict[str, Any]] = []
    verified: list[str] = []
    expected_command = 'bash "${PLUGIN_ROOT}/scripts/run-runtime.sh" --goal-plus-host-hook'
    if installation and installation.path:
        receipt_path = installation.path / ".goal-plus-runtime.json"
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text())
            expected_command = "bash " + shlex.quote(str(Path(receipt["package"]) / "scripts/run-runtime.sh")) + " --goal-plus-host-hook"
    for event_name, event_label in HOOK_EVENT_LABELS.items():
        groups = events.get(event_name)
        if not isinstance(groups, list) or not groups:
            failures.append({"event": event_name, "reason": "missing event"})
            continue
        group = groups[0]
        handlers = group.get("hooks", []) if isinstance(group, dict) else []
        if not isinstance(handlers, list) or not handlers or not isinstance(handlers[0], dict):
            failures.append({"event": event_name, "reason": "missing command handler"})
            continue
        handler = handlers[0]
        if handler.get("type") != "command" or handler.get("command") != expected_command:
            failures.append({"event": event_name, "reason": "unexpected command"})
            continue
        current_hash = _canonical_hook_hash(event_name, group, handler)
        key = f"{key_source}:{event_label}:0:0"
        state = hook_states.get(key)
        if not isinstance(state, dict):
            failures.append(
                {"event": event_name, "reason": "untrusted", "key": key, "hash": current_hash}
            )
            continue
        if state.get("enabled") is False:
            failures.append({"event": event_name, "reason": "disabled", "key": key})
            continue
        if state.get("trusted_hash") != current_hash:
            failures.append(
                {
                    "event": event_name,
                    "reason": "modified or stale trust hash",
                    "key": key,
                    "current_hash": current_hash,
                    "trusted_hash": state.get("trusted_hash"),
                }
            )
            continue
        verified.append(event_name)
    if failures:
        return CheckResult(
            "codex.hooks",
            "fail",
            "Goal Plus hooks are missing, disabled, untrusted, or modified.",
            {
                "hooks_path": str(hooks_path),
                "source": source_kind,
                "verified": verified,
                "failures": failures,
                "fix": "Open /hooks, review Goal Plus, then trust every required hook.",
            },
        )
    return CheckResult(
        "codex.hooks",
        "pass",
        "All six Goal Plus lifecycle hooks are present and trusted at their current hashes.",
        {
            "hooks_path": str(hooks_path),
            "source": source_kind,
            "events": verified,
            "trusted_project": trusted_ancestor,
        },
    )


def _list_codex_models() -> list[dict[str, Any]]:
    from .agent_hosts import host_model_catalog

    return host_model_catalog("codex").list_available_models()


def _check_models(*, skip: bool) -> CheckResult:
    if skip:
        return CheckResult(
            "codex.models",
            "info",
            "Codex model discovery was skipped by request.",
            {},
        )
    try:
        models = _list_codex_models()
    except Exception as exc:  # The adapter normalizes protocol errors at this boundary.
        return CheckResult(
            "codex.models",
            "fail",
            f"Codex model discovery failed: {exc}",
            {"fix": "Run `codex login`, then rerun the doctor."},
        )
    if not models:
        return CheckResult(
            "codex.models",
            "fail",
            "Codex returned no discoverable models.",
            {},
        )
    return CheckResult(
        "codex.models",
        "pass",
        f"Codex exposed {len(models)} selectable model(s).",
        {"models": [item.get("model") or item.get("model_id") for item in models[:10]]},
    )


def _check_packaged_assets(source_root: Path) -> CheckResult:
    assets = source_root / "assets/codex"
    required = (
        assets / "plugin.json",
        assets / "mcp.json",
        assets / "hooks.json",
        source_root / "assets/goal-plus-instructions.txt",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        return CheckResult(
            "goal_plus.package_assets",
            "fail",
            "The Goal Plus Codex plugin package is incomplete.",
            {"missing": missing},
        )
    return CheckResult(
        "goal_plus.package_assets",
        "pass",
        "Plugin manifest, MCP declaration, and default hook declaration are present.",
        {"source_root": str(source_root)},
    )


def collect_report(
    *,
    project_root: Path | None = None,
    codex_home: Path | None = None,
    skip_model_check: bool = False,
) -> DoctorReport:
    project_root = (project_root or Path.cwd()).expanduser().resolve()
    codex_home = (
        codex_home
        or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    ).expanduser().resolve()
    source_root = _package_root()
    plugin_check, installation = _check_plugin(
        codex_home,
        project_root=project_root,
    )
    checks = (
        _check_codex_version(codex_home),
        _check_goal_plus_cli(codex_home, installation.path if installation and installation.path else source_root),
        _check_packaged_assets(source_root),
        plugin_check,
        _check_agent_roles(installation=installation, project_root=project_root),
        _check_mcp(codex_home),
        _check_hooks(
            codex_home=codex_home,
            project_root=project_root,
            installation=installation,
        ),
        _check_models(skip=skip_model_check),
        CheckResult(
            "codex.session_reload",
            "info",
            "Use a fresh Codex thread after install/update and again after hook trust changes.",
            {},
        ),
    )
    return DoctorReport(
        checks=checks,
        project_root=str(project_root),
        codex_home=str(codex_home),
        source_root=str(source_root),
    )


def _render_text(report: DoctorReport) -> str:
    payload = report.to_dict()
    lines = ["Goal Plus Codex doctor"]
    for check in report.checks:
        lines.append(f"{check.status.upper():4}  {check.name}: {check.summary}")
        for key, value in check.details.items():
            if value in (None, "", [], {}):
                continue
            rendered = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
            lines.append(f"      {key}: {rendered}")
    lines.extend(
        (
            f"Overall: {payload['status']}",
            f"Next: {payload['next_action']}",
        )
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the Goal Plus Codex installation.")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Codex workspace whose project-local assets/trust should be checked.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=None,
        help="Codex home directory (default: CODEX_HOME or ~/.codex).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument(
        "--skip-model-check",
        action="store_true",
        help="Skip the read-only Codex app-server model/list probe.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = collect_report(
        project_root=args.project_root,
        codex_home=args.codex_home,
        skip_model_check=args.skip_model_check,
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_render_text(report))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
