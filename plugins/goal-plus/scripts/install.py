#!/usr/bin/env python3
"""Install Goal Plus for Codex, Pi, or an isolated Pi development checkout."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import managed_runtime

ROOT = SCRIPT_DIR.parent
DEV_SCHEMA = "goal-plus.pi-dev-installation.v1"
DEV_REGISTRY_SCHEMA = "goal-plus.pi-dev-registry-entry.v1"
NORMAL_CODEX_PLUGIN_ID = "goal-plus@goal-plus"
NORMAL_CODEX_MARKETPLACE = "goal-plus"
THINKTHREAD_PROFILE_NAME = "goal-plus-pi"
THINKTHREAD_PROFILE_MARKER = "# Managed by Goal Plus: thinkthread-profile-v1"
THINKTHREAD_RUNTIME_VERSION = "thinkthread 0.1.0"
THINKTHREAD_PROFILE_SCHEMA = 4
THINKTHREAD_EXTENSION_PROTOCOL = 5
AGENT_POSIX_CONTRACT_FINGERPRINT = (
    "780479f40a2e93789bdca94033da18189f406c704e1d35a8e67052f0fe8ecfd6"
)
AGENT_POSIX_CONTROL_PROTOCOL = 2
AGENT_POSIX_REPOSITORY = "https://gitcode.com/aideveloper/capsule_public.git"
AGENT_POSIX_REF = "v0.1.0-alpha9"
THINKTHREAD_SDK_RECEIPT_SCHEMA = "goal-plus.thinkthread-agent-posix.v1"


class InstallError(RuntimeError):
    """A safe, actionable installation failure."""


@dataclass(frozen=True)
class ThinkThreadProfilePlan:
    executable: str
    runtime_version: str
    path: Path
    content: str
    installed: bool
    native_agent: str = "pi"


@dataclass(frozen=True)
class ThinkThreadCodexConfigPlan:
    directory: Path
    content: str
    auth_source: Path | None
    catalog_source: Path | None = None


@dataclass(frozen=True)
class ThinkThreadSdkPlan:
    node_executable: str
    npm_executable: str
    git_executable: str | None
    install_root: Path
    entry: Path
    source: Path | None
    package: Path | None
    repository: str
    ref: str
    installed: bool


def _run(
    argv: list[str],
    *,
    capture: bool = False,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    stdin: int | None = None,
) -> subprocess.CompletedProcess[str]:
    if not capture:
        print("+", shlex.join(argv))
    if dry_run and not capture:
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return subprocess.run(
        argv,
        check=True,
        capture_output=capture,
        text=True,
        env=env,
        cwd=cwd,
        stdin=stdin,
    )


def _require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise InstallError(f"required executable is not on PATH: {name}")
    return str(Path(executable).resolve())


def _report_sandbox_support(*, codex: bool, pi: bool) -> None:
    if codex:
        print("Sandbox (Codex direct): native workspace-write/read-only policies enabled.")
    if not pi:
        return
    backend = (
        "bwrap" if sys.platform.startswith("linux")
        else "/usr/bin/sandbox-exec" if sys.platform == "darwin"
        else None
    )
    if backend is not None and shutil.which(backend):
        print(
            f"Sandbox (Pi direct): {backend} detected; "
            "OS permissions are checked at worker launch."
        )
    else:
        print(
            f"warning: Pi direct sandbox backend {backend or sys.platform} is unavailable. "
            "Installation can continue; protected workers will fail to launch. "
            "No unsandboxed fallback or system dependency installation is performed. "
            "See docs/sandbox.md.",
            file=sys.stderr,
        )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise InstallError(f"could not read JSON from {path}: {exc}") from exc


def _atomic_write(path: Path, content: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        temporary.chmod(mode)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _thinkthread_profile_path(native_agent: str = "pi") -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    config_root = (
        Path(configured).expanduser() if configured else Path.home() / ".config"
    )
    return config_root.resolve() / "thinkthread" / "profiles" / (
        f"goal-plus-{native_agent}.toml"
    )


def _thinkthread_sdk_entry() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    data_root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return (
        data_root.resolve()
        / "goal-plus"
        / "thinkthread-agent-posix"
        / AGENT_POSIX_CONTRACT_FINGERPRINT
        / "node_modules"
        / "@thinkthread"
        / "agent-posix"
        / "dist"
        / "index.js"
    )


def _thinkthread_sdk_root() -> Path:
    return _thinkthread_sdk_entry().parents[4]


def _thinkthread_sdk_receipt(root: Path | None = None) -> Path:
    return (root or _thinkthread_sdk_root()) / "installation.json"


def _managed_thinkthread_sdk_source(root: Path) -> Path | None:
    receipt = _thinkthread_sdk_receipt(root)
    if not receipt.is_file():
        return None
    payload = _read_json(receipt)
    if not isinstance(payload, dict) or payload.get("schema") != THINKTHREAD_SDK_RECEIPT_SCHEMA:
        return None
    source = payload.get("source_path")
    return Path(source).expanduser().resolve() if isinstance(source, str) else None


def _validate_agent_posix_sdk(node_executable: str, entry: Path) -> None:
    script = """
import { pathToFileURL } from "node:url";
const sdk = await import(pathToFileURL(process.argv[1]).href);
process.stdout.write(JSON.stringify({
  protocol: sdk.CONTROL_PROTOCOL_VERSION,
  fingerprint: sdk.CONTRACT_FINGERPRINT,
}));
""".strip()
    try:
        completed = _run(
            [node_executable, "--input-type=module", "-e", script, str(entry)],
            capture=True,
        )
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, subprocess.CalledProcessError, OSError) as exc:
        raise InstallError(f"could not validate Agent POSIX SDK at {entry}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstallError("Agent POSIX SDK metadata must be an object")
    if (
        payload.get("protocol") != AGENT_POSIX_CONTROL_PROTOCOL
        or payload.get("fingerprint") != AGENT_POSIX_CONTRACT_FINGERPRINT
    ):
        print(
            "warning: Agent POSIX SDK differs from the tested baseline: "
            f"protocol={payload.get('protocol')!r}, "
            f"fingerprint={payload.get('fingerprint')!r}",
            file=sys.stderr,
        )


def _preflight_thinkthread_sdk(
    *,
    node_executable: str,
    npm_executable: str,
    git_executable: str | None,
    source: Path | None = None,
    package: Path | None = None,
    install_root: Path | None = None,
    allow_replacement: bool = False,
) -> ThinkThreadSdkPlan:
    if source is not None and package is not None:
        raise InstallError("choose either --agent-posix-source or --agent-posix-package")
    resolved_source = source.expanduser().resolve() if source is not None else None
    resolved_package = package.expanduser().resolve() if package is not None else None
    if resolved_source is not None and not (resolved_source / "package.json").is_file():
        raise InstallError(f"Agent POSIX source has no package.json: {resolved_source}")
    if resolved_package is not None and not resolved_package.is_file():
        raise InstallError(f"Agent POSIX package is unavailable: {resolved_package}")
    if resolved_source is None and resolved_package is None and git_executable is None:
        raise InstallError("git is required to fetch the supported Agent POSIX SDK")

    root = (install_root or _thinkthread_sdk_root()).expanduser().resolve()
    entry = (
        root
        / "node_modules"
        / "@thinkthread"
        / "agent-posix"
        / "dist"
        / "index.js"
    )
    installed = False
    if root.exists():
        owner = _managed_thinkthread_sdk_source(root)
        foreign = owner != ROOT.resolve()
        if foreign and not allow_replacement:
            detail = f"managed by {owner}" if owner is not None else "not Goal Plus-managed"
            raise InstallError(
                f"Agent POSIX SDK directory is {detail}: {root}\n"
                "Remove or relocate it explicitly, then rerun the installer."
            )
        if foreign and allow_replacement:
            detail = f"managed by {owner}" if owner is not None else "unmanaged"
            print(
                f"warning: replacing {detail} Agent POSIX SDK at {root}",
                file=sys.stderr,
            )
        if entry.is_file() and not foreign:
            _validate_agent_posix_sdk(node_executable, entry)
            installed = True
    return ThinkThreadSdkPlan(
        node_executable=node_executable,
        npm_executable=npm_executable,
        git_executable=git_executable,
        install_root=root,
        entry=entry,
        source=resolved_source,
        package=resolved_package,
        repository=AGENT_POSIX_REPOSITORY,
        ref=AGENT_POSIX_REF,
        installed=installed,
    )


def _install_thinkthread_sdk(plan: ThinkThreadSdkPlan, *, dry_run: bool) -> None:
    if plan.installed:
        print(f"Agent POSIX SDK already matches the supported contract: {plan.entry}")
        return
    if dry_run:
        print(f"Would install the supported Agent POSIX SDK: {plan.install_root}")
        return

    plan.install_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".thinkthread-agent-posix.", dir=plan.install_root.parent)
    )
    payload = temporary / "payload"
    backup = temporary / "previous"
    try:
        payload.mkdir()
        package = plan.package
        if package is None:
            if plan.source is None:
                assert plan.git_executable is not None
                checkout = temporary / "checkout"
                _run(
                    [
                        plan.git_executable,
                        "clone",
                        "--quiet",
                        "--depth",
                        "1",
                        "--branch",
                        plan.ref,
                        plan.repository,
                        str(checkout),
                    ]
                )
                source = checkout / "sdk" / "agent-posix" / "ts"
            else:
                source = plan.source
            if not (source / "package-lock.json").is_file():
                raise InstallError(
                    f"Agent POSIX source has no package-lock.json: {source}"
                )
            build_source = temporary / "sdk-source"
            shutil.copytree(source, build_source)
            _run([plan.npm_executable, "ci"], cwd=build_source)
            package_dir = temporary / "packages"
            package_dir.mkdir()
            _run(
                [
                    plan.npm_executable,
                    "pack",
                    "--pack-destination",
                    str(package_dir),
                ],
                cwd=build_source,
            )
            packages = sorted(package_dir.glob("*.tgz"))
            if len(packages) != 1:
                raise InstallError("npm pack did not create one Agent POSIX package")
            package = packages[0]

        _atomic_write(
            payload / "package.json",
            json.dumps({"private": True}, indent=2) + "\n",
        )
        _run(
            [
                plan.npm_executable,
                "install",
                "--prefix",
                str(payload),
                "--omit=dev",
                "--no-audit",
                "--no-fund",
                str(package),
            ]
        )
        payload_entry = payload / plan.entry.relative_to(plan.install_root)
        _validate_agent_posix_sdk(plan.node_executable, payload_entry)
        _atomic_write(
            _thinkthread_sdk_receipt(payload),
            json.dumps(
                {
                    "schema": THINKTHREAD_SDK_RECEIPT_SCHEMA,
                    "source_path": str(ROOT.resolve()),
                    "repository": plan.repository,
                    "ref": plan.ref,
                    "control_protocol": AGENT_POSIX_CONTROL_PROTOCOL,
                    "contract_fingerprint": AGENT_POSIX_CONTRACT_FINGERPRINT,
                },
                indent=2,
            )
            + "\n",
        )
        if plan.install_root.exists():
            plan.install_root.replace(backup)
        payload.replace(plan.install_root)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if backup.exists() and not plan.install_root.exists():
            backup.replace(plan.install_root)
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    print(f"Installed supported Agent POSIX SDK: {plan.entry}")


def _toml_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InstallError("ThinkThread Profile contains a non-finite number")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if any(isinstance(item, (dict, list)) for item in value):
            raise InstallError(
                "ThinkThread Profile contains an unsupported nested array"
            )
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise InstallError(
        f"ThinkThread Profile contains an unsupported value: {type(value).__name__}"
    )


def _render_toml_table(
    values: dict[str, Any],
    *,
    prefix: tuple[str, ...] = (),
) -> list[str]:
    lines: list[str] = []
    scalars = [
        (key, value) for key, value in values.items() if not isinstance(value, dict)
    ]
    tables = [
        (key, value) for key, value in values.items() if isinstance(value, dict)
    ]
    if prefix:
        lines.append("[" + ".".join(_toml_key(item) for item in prefix) + "]")
    for key, value in scalars:
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, table in tables:
        if lines:
            lines.append("")
        lines.extend(_render_toml_table(table, prefix=(*prefix, key)))
    return lines


def _render_thinkthread_profile(profile: dict[str, Any]) -> str:
    body = "\n".join(_render_toml_table(profile)).rstrip()
    return (
        f"{THINKTHREAD_PROFILE_MARKER}\n"
        f"# Source checkout: {ROOT.resolve()}\n\n"
        f"{body}\n"
    )


def _parse_thinkthread_profile(text: str, *, source: str) -> dict[str, Any]:
    try:
        payload = tomllib.loads(text)
    except (TypeError, ValueError) as exc:
        raise InstallError(
            f"could not parse ThinkThread Profile from {source}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise InstallError(f"ThinkThread Profile is not an object: {source}")
    return payload


def _validate_base_thinkthread_profile(profile: dict[str, Any], *, native_agent: str = "pi") -> None:
    if native_agent not in {"pi", "codex"}:
        raise InstallError(f"unsupported ThinkThread native agent: {native_agent}")
    agent = profile.get("agent")
    extension = agent.get("extension") if isinstance(agent, dict) else None
    loader = extension.get("loader") if isinstance(extension, dict) else None
    environment = profile.get("environment")
    if profile.get("schemaVersion") != THINKTHREAD_PROFILE_SCHEMA:
        print(
            "warning: ThinkThread Pi Profile schema differs from the tested baseline: "
            f"reported={profile.get('schemaVersion')!r}, expected={THINKTHREAD_PROFILE_SCHEMA}",
            file=sys.stderr,
        )
    if not isinstance(agent, dict) or agent.get("name") != native_agent:
        raise InstallError(f"ThinkThread Profile does not describe the {native_agent} Agent")
    if not isinstance(loader, dict):
        raise InstallError("ThinkThread Pi Profile has no Agent Extension loader")
    if loader.get("protocolVersion") not in (None, THINKTHREAD_EXTENSION_PROTOCOL):
        print(
            "warning: ThinkThread Pi Agent Extension protocol declaration differs "
            f"from the tested baseline: reported={loader['protocolVersion']!r}, "
            f"expected={THINKTHREAD_EXTENSION_PROTOCOL}; Runtime validates the actual handshake",
            file=sys.stderr,
        )
    if not isinstance(environment, dict) or (
        environment.get("rootFsMode") != "direct"
        or environment.get("childFsMode") != "copy_on_write"
    ):
        raise InstallError(
            "ThinkThread Pi Profile must use rootFsMode=direct and "
            "childFsMode=copy_on_write"
        )


def _append_agent_argument(arguments: list[str], flag: str, value: Path) -> None:
    rendered = str(value.resolve())
    for index, item in enumerate(arguments[:-1]):
        if (
            item == flag
            and Path(arguments[index + 1]).expanduser().resolve() == value.resolve()
        ):
            return
    arguments.extend([flag, rendered])


def _symlink_chain_parent_roots(path: Path) -> tuple[Path, ...]:
    """Return real parent directories needed to traverse a symlink chain."""
    pending = path.expanduser().absolute()
    roots: list[Path] = []
    seen: set[tuple[Path, tuple[str, ...]]] = set()
    for _ in range(64):
        candidate = Path(pending.anchor)
        link: Path | None = None
        remainder: tuple[str, ...] = ()
        for index, part in enumerate(pending.parts[1:], start=1):
            candidate /= part
            if candidate.is_symlink():
                link = candidate
                remainder = pending.parts[index + 1 :]
                break
        if link is None:
            return tuple(roots)
        identity = (link, remainder)
        if identity in seen:
            raise InstallError(f"cyclic Python executable symlink chain: {path}")
        seen.add(identity)
        parent = link.parent.resolve()
        if parent not in roots:
            roots.append(parent)
        target = Path(os.readlink(link))
        pending = (
            target if target.is_absolute() else link.parent / target
        ).joinpath(*remainder)
    raise InstallError(f"Python executable symlink chain is too deep: {path}")


def _build_thinkthread_profile(
    base: dict[str, Any],
    *,
    node_executable: str | None = None,
    sdk_entry: Path | None = None,
    bridge_path: Path | None = None,
    package_root: Path | None = None,
    runtime_python: Path | None = None,
    codex_config_dir: Path | None = None,
) -> dict[str, Any]:
    package_root = package_root or ROOT
    # JSON provides a strict deep copy for the Profile's JSON-compatible TOML values.
    profile = json.loads(json.dumps(base))
    agent = profile.setdefault("agent", {})
    arguments = list(agent.get("args") or [])
    native_agent = agent.get("name")
    _validate_base_thinkthread_profile(base, native_agent=native_agent)
    if native_agent == "codex":
        if codex_config_dir is None or not codex_config_dir.is_absolute():
            raise InstallError("ThinkThread Codex requires its managed absolute source config directory")
        # The native Extension consumes this exact source-config variable.
        agent["configDirEnv"] = "CODEX_CONFIG_DIR"
        agent["configDirDefault"] = str(codex_config_dir)
        agent["configDirAccess"] = "read_only"
    else:
        _append_agent_argument(arguments, "--extension", package_root / "assets/pi/extensions/goal-plus.ts")
    agent["args"] = arguments

    python_prefix = runtime_python.parent.parent if runtime_python else Path(sys.prefix).expanduser().resolve()
    python_executable = runtime_python or Path(sys.executable).expanduser().absolute()
    resolved_python_prefix = python_executable.resolve().parent.parent
    resolved_node = node_executable or shutil.which("node")
    if resolved_node is None:
        raise InstallError("Node.js is required for the ThinkThread Pi Profile")
    node_prefix = Path(resolved_node).expanduser().resolve().parent.parent
    runtime_roots: list[Path] = []
    for path in (
        python_prefix,
        *_symlink_chain_parent_roots(python_executable),
        resolved_python_prefix,
        node_prefix,
    ):
        if any(path == root or path.is_relative_to(root) for root in runtime_roots):
            continue
        runtime_roots.append(path)
    if any(path == Path("/") for path in runtime_roots):
        raise InstallError(
            "refusing to expose the filesystem root for Goal Plus runtimes"
        )
    environment = profile.setdefault("environment", {})
    resolved_sdk_entry = (sdk_entry or _thinkthread_sdk_entry()).expanduser().resolve()
    resolved_bridge = (
        bridge_path
        or package_root / "src" / "goal_plus" / "assets" / "thinkthread-agent-posix-bridge.mjs"
    ).expanduser().resolve()
    readable = [
        str(Path(item).expanduser().resolve())
        for item in environment.get("read", [])
    ]
    for path in (
        package_root.resolve(),
        resolved_sdk_entry.parent.parent.parent.parent,
        *runtime_roots,
    ):
        if str(path) not in readable:
            readable.append(str(path))
    if native_agent == "codex":
        # The sandbox mounts /etc/ssl behind symlinks whose targets live elsewhere
        # (for example /etc/pki on Oracle/RHEL layouts); expose the resolved system
        # CA bundles so the native Codex Child can reach HTTPS model providers such
        # as DeepSeek.
        for path in _resolve_system_ca_bundles():
            if str(path) not in readable:
                readable.append(str(path))
    environment["read"] = readable

    session_runtime_root = ("${CODEX_HOME}/goal-plus" if native_agent == "codex"
                            else "${PI_CODING_AGENT_SESSION_DIR}/goal-plus")
    for role in ("root", "child"):
        role_environment = dict(environment.get(role) or {})
        role_environment.update(
            {
                "GOAL_PLUS_PYTHON": str(python_executable),
                "GOAL_PLUS_NODE": str(Path(resolved_node).expanduser().resolve()),
                "GOAL_PLUS_ROOT": session_runtime_root,
                "GOAL_PLUS_SOURCE_PATH": str(package_root.resolve()),
                "GOAL_PLUS_AGENT_POSIX_SDK_ENTRY": str(resolved_sdk_entry),
            }
        )
        if native_agent == "codex":
            role_environment["GOAL_PLUS_THINKTHREAD_ROLE"] = "main" if role == "root" else "candidate"
        else:
            role_environment["GOAL_PLUS_PI_ROLE"] = "main" if role == "root" else "worker"
        if role == "root":
            role_environment["GOAL_PLUS_AGENT_POSIX_BRIDGE"] = str(resolved_bridge)
        environment[role] = role_environment
    return profile


_SYSTEM_CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu/Gentoo
    "/etc/ssl/certs/ca-bundle.crt",  # Oracle/RHEL symlinked layout
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora/RHEL
    "/etc/ssl/ca-bundle.pem",  # OpenSUSE
    "/etc/pki/tls/cacert.pem",  # OpenELEC
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # CentOS/RHEL 7+
    "/etc/ssl/cert.pem",  # Alpine
)


def _resolve_system_ca_bundles(
    candidates: tuple[str, ...] = _SYSTEM_CA_BUNDLE_CANDIDATES,
) -> list[Path]:
    resolved: list[Path] = []
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_file():
            continue
        resolved_path = path.resolve()
        if resolved_path not in resolved:
            resolved.append(resolved_path)
    return resolved


def _build_thinkthread_codex_config(
    source: dict[str, Any], *, runtime_python: Path,
    codex_env_vars: tuple[str, ...] = (),
) -> dict[str, Any]:
    # Native model/provider configuration is shared; Root MCPs, plugins and instructions are not Child inputs.
    fields = {"model", "model_provider", "model_providers", "model_reasoning_effort", "service_tier", "model_catalog_json"}
    config = json.loads(json.dumps({key: value for key, value in source.items() if key in fields}))
    config["mcp_servers"] = {
        "goal-plus": {
            "command": str(runtime_python),
            "args": ["-I", "-m", "goal_plus.server"],
            "env_vars": [
                "GOAL_PLUS_THINKTHREAD_ROLE", "GOAL_PLUS_ROOT", "GOAL_PLUS_NODE",
                "GOAL_PLUS_AGENT_POSIX_SDK_ENTRY", "GOAL_PLUS_AGENT_POSIX_BRIDGE",
                # TT assigns each actor its own writable native session home.
                # Fork checkpoint inspection must read this actor's rollout.
                "CODEX_HOME",
                # The managed Profile supplies this value through configDirDefault;
                # forward it to the Goal Plus MCP so its configuration preflight
                # inspects the same isolated Codex config as the Child.
                "CODEX_CONFIG_DIR",
                "THINKTHREAD_PARENT_ID", "THINKTHREAD_CONTROL_SOCKET", "THINKTHREAD_CONTROL_TOKEN",
                "GOAL_PLUS_DEBUG",
                "GOAL_PLUS_TMPDIR",
                "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED",
                "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED",
            ],
            "startup_timeout_sec": 60,
            "tool_timeout_sec": 180,
        },
    }
    config["mcp_servers"]["goal-plus"]["env_vars"] = sorted(
        set(config["mcp_servers"]["goal-plus"]["env_vars"]) | set(codex_env_vars)
    )
    # ThinkThread runs one isolated Codex Child per actor.  Disable the shared
    # app-server daemon inside the Child; TT owns the process lifetime and a
    # detached daemon cannot be registered in the protected Child scope.
    config["features"] = {"multi_agent": False, "daemon_auto_start": False}
    return config


def _load_codex_config(source: Path, *, profile: str | None = None) -> dict[str, Any]:
    config_path = source / "config.toml"
    config = tomllib.loads(config_path.read_text()) if config_path.is_file() else {}
    if profile is None:
        return config
    # Codex profile layers are top-level key overlays on the base config; nested
    # tables such as model_providers merge per key.
    overlay_path = source / f"{profile}.config.toml"
    if not overlay_path.is_file():
        raise InstallError(f"Codex profile config not found: {overlay_path}")
    for key, value in tomllib.loads(overlay_path.read_text()).items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            merged = dict(config[key])
            merged.update(value)
            config[key] = merged
        else:
            config[key] = value
    return config


def _plan_thinkthread_codex_config(
    package: Path, source: Path | None = None, *, profile: str | None = None,
    codex_env_vars: tuple[str, ...] = (),
) -> ThinkThreadCodexConfigPlan:
    source = source or _codex_config()
    config = _load_codex_config(source, profile=profile)
    projected = _build_thinkthread_codex_config(
        config,
        runtime_python=package.parent / "venv/bin/python",
        codex_env_vars=codex_env_vars,
    )
    catalog = projected.get("model_catalog_json")
    catalog_source = None
    if isinstance(catalog, str) and catalog.strip():
        # The Child sandbox only exposes whitelisted paths, and the user's config home
        # commonly sits behind symlinked directories; project the resolved catalog
        # location so Codex opens the exact whitelisted file instead of an absent
        # literal path (which would reject the whole managed config).
        candidate = Path(catalog).expanduser()
        catalog_source = (source / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if not catalog_source.is_file():
            raise InstallError(f"Codex model catalog JSON is unavailable: {catalog_source}")
        projected["model_catalog_json"] = str(catalog_source)
    content = "\n".join(_render_toml_table(projected)).rstrip() + "\n"
    auth = (source / "auth.json").resolve() if (source / "auth.json").is_file() else None
    identity = hashlib.sha256((content + str(auth)).encode()).hexdigest()
    return ThinkThreadCodexConfigPlan(package.parent / "thinkthread-codex-config" / identity, content, auth, catalog_source)


def _install_thinkthread_codex_config(plan: ThinkThreadCodexConfigPlan, *, dry_run: bool) -> None:
    directory = plan.directory
    marker = directory / ".goal-plus-owner.json"
    ownership = {"source": str(ROOT.resolve()), "kind": "thinkthread-codex-config-v1"}
    if directory.exists() and (not marker.is_file() or _read_json(marker) != ownership):
        raise InstallError(f"refusing to replace an unowned ThinkThread Codex config: {directory}")
    if dry_run:
        print(f"Would prepare ThinkThread Codex config: {directory}")
        return
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(marker, json.dumps(ownership) + "\n", mode=0o600)
    _atomic_write(directory / "config.toml", plan.content, mode=0o600)
    if plan.auth_source is not None:
        target = directory / "auth.json"
        if target.is_symlink() and target.resolve() == plan.auth_source:
            return
        if target.exists() or target.is_symlink():
            raise InstallError(f"refusing to replace a different Codex auth binding: {target}")
        target.symlink_to(plan.auth_source)


def _managed_thinkthread_profile_source(path: Path) -> Path | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if not text.startswith(THINKTHREAD_PROFILE_MARKER + "\n"):
        return None
    payload = _parse_thinkthread_profile(text, source=str(path))
    environment = payload.get("environment")
    root_environment = (
        environment.get("root") if isinstance(environment, dict) else None
    )
    source = (
        root_environment.get("GOAL_PLUS_SOURCE_PATH")
        if isinstance(root_environment, dict)
        else None
    )
    return managed_runtime.source_owner(Path(source).expanduser()) if isinstance(source, str) else None


def _preflight_thinkthread_profile(
    executable: str,
    *,
    profile_path: Path | None = None,
    node_executable: str | None = None,
    sdk_entry: Path | None = None,
    package_root: Path | None = None,
    runtime_python: Path | None = None,
    native_agent: str = "pi",
    codex_config: ThinkThreadCodexConfigPlan | None = None,
    allow_replacement: bool = False,
) -> ThinkThreadProfilePlan:
    runtime_version = _run([executable, "--version"], capture=True).stdout.strip()
    if runtime_version != THINKTHREAD_RUNTIME_VERSION:
        print(
            "warning: ThinkThread Runtime differs from the tested baseline: "
            f"reported={runtime_version!r}, expected={THINKTHREAD_RUNTIME_VERSION!r}",
            file=sys.stderr,
        )
    raw_profile = _run(
        [executable, "profile", "show", native_agent],
        capture=True,
    ).stdout
    base = _parse_thinkthread_profile(raw_profile, source=f"tt profile show {native_agent}")
    _validate_base_thinkthread_profile(base, native_agent=native_agent)
    profile = _build_thinkthread_profile(
            base,
            node_executable=node_executable,
            sdk_entry=sdk_entry,
            package_root=package_root,
            runtime_python=runtime_python,
            codex_config_dir=codex_config.directory if codex_config is not None else None,
        )
    if codex_config is not None and codex_config.auth_source is not None:
        profile["environment"]["read"].append(str(codex_config.auth_source))
    if codex_config is not None and codex_config.catalog_source is not None:
        profile["environment"]["read"].append(str(codex_config.catalog_source))
    content = _render_thinkthread_profile(profile)
    path = (profile_path or (_thinkthread_profile_path() if native_agent == "pi"
                             else _thinkthread_profile_path(native_agent))).expanduser().resolve()
    installed = False
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        owner = _managed_thinkthread_profile_source(path)
        foreign = owner != ROOT.resolve()
        if existing == content and not foreign:
            installed = True
        elif foreign and not allow_replacement:
            detail = (
                f"managed by {owner}"
                if owner is not None
                else "not managed by this installer"
            )
            raise InstallError(
                f"ThinkThread Profile already exists and is {detail}: {path}\n"
                "Resolve that Profile outside Goal Plus, then rerun the installer."
            )
        elif foreign and allow_replacement:
            detail = f"managed by {owner}" if owner is not None else "unmanaged"
            print(
                f"warning: replacing {detail} ThinkThread Profile at {path}",
                file=sys.stderr,
            )
    return ThinkThreadProfilePlan(
        executable=executable,
        runtime_version=runtime_version,
        path=path,
        content=content,
        installed=installed,
        native_agent=native_agent,
    )


def _install_thinkthread_profile(
    plan: ThinkThreadProfilePlan,
    *,
    dry_run: bool,
) -> None:
    if plan.installed:
        print(f"ThinkThread Profile already points to this checkout: {plan.path}")
    elif dry_run:
        action = "update" if plan.path.exists() else "write"
        print(f"Would {action}: {plan.path}")
    else:
        original: str | None = None
        original_mode = 0o644
        if plan.path.exists():
            with plan.path.open(encoding="utf-8", newline="") as handle:
                original = handle.read()
                original_mode = os.fstat(handle.fileno()).st_mode & 0o7777
        _atomic_write(plan.path, plan.content)
        try:
            rendered = _run(
                [plan.executable, "profile", "show", f"goal-plus-{plan.native_agent}"],
                capture=True,
            ).stdout
            installed = _parse_thinkthread_profile(
                rendered,
                source=f"tt profile show {THINKTHREAD_PROFILE_NAME}",
            )
            _validate_base_thinkthread_profile(installed, native_agent=plan.native_agent)
        except (Exception, KeyboardInterrupt) as exc:
            try:
                if original is None:
                    plan.path.unlink(missing_ok=True)
                else:
                    _atomic_write(plan.path, original, mode=original_mode)
            except (Exception, KeyboardInterrupt) as rollback_error:
                raise InstallError(
                    f"ThinkThread Profile validation failed ({exc!r}); "
                    f"rollback failed for {plan.path}: {rollback_error!r}"
                ) from exc
            raise
    print(
        "ThinkThread integration is ready. Start a Root with: "
        f"tt run --profile goal-plus-{plan.native_agent} --fs <target-workspace>"
    )


def _pi_dev_registry_dir() -> Path:
    configured = os.environ.get("GOAL_PLUS_INSTALL_STATE_DIR")
    if configured:
        state_root = Path(configured).expanduser()
    else:
        xdg_state = os.environ.get("XDG_STATE_HOME")
        state_root = (
            Path(xdg_state).expanduser()
            if xdg_state
            else Path.home() / ".local" / "state"
        ) / "goal-plus"
    return state_root.resolve() / "pi-dev-environments"


def _pi_dev_registry_path(dev_dir: Path) -> Path:
    digest = hashlib.sha256(str(dev_dir.resolve()).encode("utf-8")).hexdigest()[:20]
    return _pi_dev_registry_dir() / f"{digest}.json"


def _register_pi_dev_environment(dev_dir: Path) -> None:
    dev_dir = dev_dir.resolve()
    payload = {
        "schema": DEV_REGISTRY_SCHEMA,
        "dev_dir": str(dev_dir),
        "receipt_path": str(dev_dir / "installation.json"),
        "source_path": str(ROOT.resolve()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write(
        _pi_dev_registry_path(dev_dir),
        json.dumps(payload, indent=2) + "\n",
    )


def _registered_pi_dev_environments() -> tuple[dict[Path, dict[str, Any]], list[str]]:
    records: dict[Path, dict[str, Any]] = {}
    warnings: list[str] = []
    registry_dir = _pi_dev_registry_dir()
    if not registry_dir.is_dir():
        return records, warnings
    for path in sorted(registry_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema") != DEV_REGISTRY_SCHEMA
                or not isinstance(payload.get("dev_dir"), str)
            ):
                raise ValueError("unrecognized registry entry")
            dev_dir = Path(payload["dev_dir"]).expanduser().resolve()
            records[dev_dir] = payload
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            warnings.append(f"could not inspect registry entry {path}: {exc}")
    return records, warnings


def _git_worktree_pi_dev_dirs() -> set[Path]:
    discovered = {(ROOT / ".goal-plus-dev").resolve()}
    try:
        repository_root = Path(
            _run(
                ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
                capture=True,
            ).stdout.strip()
        ).resolve()
        plugin_subdir = ROOT.resolve().relative_to(repository_root)
        listing = _run(
            ["git", "-C", str(ROOT), "worktree", "list", "--porcelain"],
            capture=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError, ValueError):
        return discovered
    for line in listing.splitlines():
        if line.startswith("worktree "):
            worktree = Path(line.removeprefix("worktree ")).resolve()
            discovered.add((worktree / plugin_subdir / ".goal-plus-dev").resolve())
    return discovered


def _allocated_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            stat = current.lstat()
            blocks = getattr(stat, "st_blocks", 0)
            total += int(blocks) * 512 if blocks else int(stat.st_size)
            if current.is_dir() and not current.is_symlink():
                pending.extend(current.iterdir())
        except OSError:
            continue
    return total


def _human_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    raise AssertionError("unreachable")


def _dirty_label(value: Any) -> str:
    if value is True:
        return "dirty"
    if value is False:
        return "clean"
    return "unknown"


def _git_head(path: Path) -> str | None:
    try:
        return _run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture=True,
        ).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_dirty(path: Path) -> bool | None:
    try:
        return bool(
            _run(
                ["git", "-C", str(path), "status", "--porcelain"],
                capture=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return None


def _python_version(python: Path) -> str | None:
    if not python.is_file():
        return None
    try:
        result = subprocess.run(
            [str(python), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return (result.stdout or result.stderr).strip() or None


def _inspect_pi_dev_environment(
    dev_dir: Path,
    registry_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dev_dir = dev_dir.resolve()
    receipt_path = dev_dir / "installation.json"
    receipt: dict[str, Any] = {}
    receipt_error: str | None = None
    if receipt_path.is_file():
        try:
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema") != DEV_SCHEMA:
                raise ValueError("unrecognized installation receipt")
            receipt = payload
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            receipt_error = str(exc)

    source_value = receipt.get("source_path") or (registry_record or {}).get(
        "source_path"
    )
    source = Path(str(source_value)).expanduser().resolve() if source_value else None
    venv_python_value = receipt.get("venv_python")
    venv_python = (
        Path(str(venv_python_value)).expanduser().resolve()
        if venv_python_value
        else _venv_python(dev_dir / "venv")
    )
    launcher_value = receipt.get("launcher")
    launcher = (
        Path(str(launcher_value)).expanduser().resolve()
        if launcher_value
        else dev_dir / "bin" / "pi-goal-plus"
    )
    if not dev_dir.exists():
        status = "missing"
    elif receipt_error:
        status = "invalid-receipt"
    elif not receipt:
        status = "unmanaged"
    elif (
        receipt.get("status") != "ready"
        or not venv_python.is_file()
        or not launcher.is_file()
    ):
        status = "incomplete"
    elif source is None or not source.is_dir():
        status = "source-missing"
    else:
        status = "ready"
    return {
        "status": status,
        "dev_dir": dev_dir,
        "source": source,
        "size_bytes": _allocated_size_bytes(dev_dir),
        "prepared_commit": receipt.get("source_commit"),
        "prepared_dirty": receipt.get("source_dirty"),
        "source_head": _git_head(source) if source and source.is_dir() else None,
        "source_dirty": _git_dirty(source) if source and source.is_dir() else None,
        "python_version": _python_version(venv_python),
        "receipt_error": receipt_error,
    }


def _show_pi_dev_environments(*, current_dev_dir: Path) -> None:
    registered, warnings = _registered_pi_dev_environments()
    candidates = set(registered)
    candidates.update(_git_worktree_pi_dev_dirs())
    candidates.add(current_dev_dir.expanduser().resolve())
    rows = [
        _inspect_pi_dev_environment(path, registered.get(path))
        for path in sorted(candidates, key=str)
        if path.exists() or path in registered
    ]
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"Pi development environments: {len(rows)}")
    for row in rows:
        prepared = str(row["prepared_commit"] or "unknown")[:12]
        current = str(row["source_head"] or "unknown")[:12]
        prepared_dirty = _dirty_label(row["prepared_dirty"])
        current_dirty = _dirty_label(row["source_dirty"])
        print(f"- [{row['status']}] {_human_size(int(row['size_bytes']))}")
        print(f"  source: {row['source'] or 'unknown'}")
        print(f"  environment: {row['dev_dir']}")
        print(
            f"  source commit: prepared={prepared} ({prepared_dirty}), "
            f"current={current} ({current_dirty})"
        )
        print(f"  python: {row['python_version'] or 'unavailable'}")
        if row["receipt_error"]:
            print(f"  receipt error: {row['receipt_error']}")
    existing = sum(1 for row in rows if row["status"] != "missing")
    missing = len(rows) - existing
    total_size = sum(int(row["size_bytes"]) for row in rows)
    print(
        f"Total: {existing} existing, {missing} missing, "
        f"{_human_size(total_size)} allocated"
    )


def _codex_inventory(codex: str) -> list[dict[str, Any]]:
    result = _run([codex, "plugin", "list", "--json"], capture=True)
    try:
        payload = json.loads(result.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise InstallError("Codex returned invalid plugin inventory JSON") from exc
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, list):
        raise InstallError("Codex plugin inventory did not contain an installed list")
    return [item for item in installed if isinstance(item, dict)]


def _codex_marketplaces(codex: str) -> dict[str, Path]:
    result = _run([codex, "plugin", "marketplace", "list"], capture=True)
    marketplaces: dict[str, Path] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("MARKETPLACE"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        marketplaces[parts[0]] = Path(parts[1]).expanduser().resolve()
    return marketplaces


def _preflight_codex(codex: str, *, allow_replacement: bool = False) -> bool:
    root = ROOT.resolve()
    conflicts: list[str] = []
    legacy_plugin_installed = False
    for item in _codex_inventory(codex):
        plugin_id = str(item.get("pluginId") or "")
        name = str(item.get("name") or "")
        if name != "goal-plus" and not plugin_id.startswith("goal-plus@"):
            continue
        source = item.get("source")
        source_path = source.get("path") if isinstance(source, dict) else None
        resolved_source = (
            Path(source_path).expanduser().resolve()
            if isinstance(source_path, str) and source_path
            else None
        )
        if plugin_id != NORMAL_CODEX_PLUGIN_ID:
            conflicts.append(
                f"installed Codex plugin {plugin_id!r} at {resolved_source or 'unknown'}"
            )
            legacy_plugin_installed = legacy_plugin_installed or (
                plugin_id == "goal-plus@goal-plus-local"
            )
        elif not allow_replacement and resolved_source is not None and managed_runtime.source_owner(resolved_source) != root:
            conflicts.append(
                f"{NORMAL_CODEX_PLUGIN_ID!r} points to {resolved_source}, expected {root}"
            )

    marketplaces = _codex_marketplaces(codex)
    marketplace_root = marketplaces.get(NORMAL_CODEX_MARKETPLACE)
    if not allow_replacement and marketplace_root is not None and managed_runtime.source_owner(marketplace_root) != root:
        conflicts.append(
            f"Codex marketplace {NORMAL_CODEX_MARKETPLACE!r} points to "
            f"{marketplace_root}, expected {root}"
        )
    legacy_marketplace_installed = "goal-plus-local" in marketplaces
    for name, path in marketplaces.items():
        if name.startswith("goal-plus") and name != NORMAL_CODEX_MARKETPLACE:
            conflicts.append(f"Codex marketplace {name!r} points to {path}")
    if conflicts:
        rendered = "\n  - ".join(conflicts)
        cleanup = ""
        if legacy_plugin_installed or legacy_marketplace_installed:
            cleanup = (
                "\nThe retired local staging install can be removed with:\n"
                "  codex plugin remove goal-plus@goal-plus-local\n"
                "  codex plugin marketplace remove goal-plus-local\n"
            )
        raise InstallError(
            "Codex already has a conflicting Goal Plus installation:\n"
            f"  - {rendered}\n"
            f"{cleanup}"
            "Remove the conflicting plugin/marketplace explicitly, then rerun the installer."
        )
    return marketplace_root is not None


def _pi_agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".pi" / "agent"
    )
    # Pi stores local package sources relative to the configured settings path.
    # Preserve that lexical anchor (for example /tmp on macOS) so
    # _local_source_path can normalize the relative source the same way Pi does.
    return Path(os.path.abspath(path))


def _package_source(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("source"), str):
        return entry["source"]
    return None


def _local_source_path(source: str, settings_path: Path) -> Path | None:
    if source.startswith(("npm:", "git:", "http://", "https://", "ssh://", "git://")):
        return None
    candidate = Path(source).expanduser()
    if not candidate.is_absolute():
        # Normalize `..` against Pi's lexical settings path before resolving
        # filesystem symlinks. On macOS, resolving /tmp to /private/tmp first
        # can otherwise turn a valid relative source into /private/Users/....
        candidate = Path(os.path.abspath(settings_path.parent / candidate))
    return candidate.resolve()


def _pi_cli_source(entry: dict[str, Any]) -> str:
    """Match Pi's lexical local-path identity, including symlinked config roots."""
    source = entry["source"]
    if entry.get("resolved_path") is None:
        return source
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path(entry["settings_path"]).parent / path
    return os.path.abspath(path)


def _source_is_goal_plus(source: str, settings_path: Path) -> tuple[bool, Path | None]:
    local_path = _local_source_path(source, settings_path)
    if local_path is not None:
        manifest = local_path / "package.json"
        if not manifest.is_file():
            manifest = local_path / "assets/pi/package.json"
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("name") == "goal-plus":
                return True, local_path
        return (
            local_path == ROOT.resolve() or local_path.name == "goal-plus",
            local_path,
        )

    normalized = source.lower()
    if normalized.startswith("npm:"):
        package = normalized.removeprefix("npm:")
        if package.startswith("@"):
            package = package.rsplit("@", 1)[0] if package.count("@") > 1 else package
        else:
            package = package.split("@", 1)[0]
        return package == "goal-plus" or package.endswith("/goal-plus"), None
    return "goal-plus" in normalized, None


def _settings_goal_plus_entries(
    settings_path: Path, *, scope: str
) -> list[dict[str, Any]]:
    if not settings_path.is_file():
        return []
    payload = _read_json(settings_path)
    if not isinstance(payload, dict):
        raise InstallError(f"Pi settings must be an object: {settings_path}")
    packages = payload.get("packages", [])
    if not isinstance(packages, list):
        raise InstallError(f"Pi settings packages must be a list: {settings_path}")
    matches: list[dict[str, Any]] = []
    for entry in packages:
        source = _package_source(entry)
        if source is None:
            continue
        is_goal_plus, local_path = _source_is_goal_plus(source, settings_path)
        if is_goal_plus:
            matches.append(
                {
                    "scope": scope,
                    "source": source,
                    "resolved_path": local_path,
                    "settings_path": settings_path,
                }
            )
    return matches


def _pi_resource_is_goal_plus(path: Path) -> bool:
    return path.name in {"goal-plus.ts", "goal-plus.js", "goal-plus"} or (
        path.name == "SKILL.md" and path.parent.name == "goal-plus"
    )


def _pi_configured_resources(settings_path: Path, *, scope: str) -> list[dict[str, Any]]:
    if not settings_path.is_file():
        return []
    payload = _read_json(settings_path)
    matches = []
    for field in ("extensions", "skills"):
        entries = payload.get(field, [])
        if not isinstance(entries, list):
            raise InstallError(f"Pi settings {field} must be a list: {settings_path}")
        for source in entries:
            if not isinstance(source, str) or source.startswith(("!", "-")):
                continue
            local = _local_source_path(source.removeprefix("+"), settings_path)
            if local is None:
                continue
            paths = [Path(p) for p in glob.glob(str(local), recursive=True)]
            for path in paths or [local]:
                candidates = [path]
                if path.is_dir() and not _pi_resource_is_goal_plus(path):
                    candidates.extend(
                        (p for p in path.iterdir() if p.suffix in {".ts", ".js"})
                        if field == "extensions" else path.rglob("SKILL.md")
                    )
                for candidate in candidates:
                    if _pi_resource_is_goal_plus(candidate):
                        matches.append({"scope": scope, "source": source,
                                        "resolved_path": candidate.resolve(),
                                        "settings_path": settings_path, "field": field,
                                        "removable": local == candidate and not glob.has_magic(source)})
    return matches


def _pi_goal_plus_entries(*, cwd: Path | None = None) -> list[dict[str, Any]]:
    agent_dir = _pi_agent_dir()
    matches = _settings_goal_plus_entries(agent_dir / "settings.json", scope="user")
    matches.extend(_pi_configured_resources(agent_dir / "settings.json", scope="user"))
    loose_paths = [
        ("user", agent_dir / "extensions" / "goal-plus.ts"),
        ("user", agent_dir / "skills" / "goal-plus"),
    ]
    if cwd is not None:
        project_pi = cwd.resolve() / ".pi"
        loose_paths.extend(
            (
                ("project", project_pi / "extensions" / "goal-plus.ts"),
                ("project", project_pi / "skills" / "goal-plus"),
            )
        )
    for scope, loose_path in loose_paths:
        # Pi does not load a broken loose-resource symlink. Treating one as an
        # active registration makes checkout-local development impossible even
        # though the host will ignore it.
        if loose_path.exists():
            matches.append(
                {
                    "scope": scope,
                    "source": str(loose_path),
                    "resolved_path": loose_path.resolve(),
                    "settings_path": None,
                }
            )
    if cwd is not None:
        project_settings = cwd.resolve() / ".pi" / "settings.json"
        matches.extend(_settings_goal_plus_entries(project_settings, scope="project"))
        matches.extend(_pi_configured_resources(project_settings, scope="project"))
    return matches


def _render_pi_entries(entries: list[dict[str, Any]]) -> str:
    lines = []
    for entry in entries:
        resolved = entry.get("resolved_path")
        suffix = f" -> {resolved}" if resolved else ""
        field = entry.get("field", "packages")
        lines.append(f"[{entry['scope']}] {field}: {entry['source']}{suffix}")
    return "\n  - ".join(lines)


def _remove_pi_resource_registrations(entries: list[dict[str, Any]], *, dry_run: bool) -> None:
    for settings in {entry["settings_path"] for entry in entries}:
        if dry_run:
            print("Would remove explicit Goal Plus resource registrations from", settings)
            continue
        payload = _read_json(settings)
        for entry in entries:
            if entry["settings_path"] == settings:
                if not entry.get("removable"):
                    raise InstallError(f"Cannot remove a shared Pi resource path: {entry['source']}")
                field = entry["field"]
                payload[field] = [source for source in payload.get(field, []) if source != entry["source"]]
        _atomic_write(settings, json.dumps(payload, indent=2) + "\n")


def _pi_conflict_uninstall_commands(entries: list[dict[str, Any]]) -> list[str]:
    """Return copyable removal commands when the conflicting owner is known."""
    commands: list[str] = []
    for entry in entries:
        resolved = entry.get("resolved_path")
        settings_path = entry.get("settings_path")
        source = entry.get("source")
        if entry.get("field") in {"extensions", "skills"}:
            continue
        if (
            settings_path is not None
            and isinstance(resolved, Path)
            and (resolved / "uninstall.sh").is_file()
        ):
            command = f"(cd {shlex.quote(str(resolved))} && ./uninstall.sh --pi)"
        elif settings_path is not None and isinstance(source, str):
            command = shlex.join(["pi", "remove", source])
        else:
            continue
        if command not in commands:
            commands.append(command)
    return commands


def _render_pi_conflict_remediation(entries: list[dict[str, Any]]) -> str:
    commands = _pi_conflict_uninstall_commands(entries)
    if not commands:
        return "Remove or disable those Goal Plus resources explicitly, then rerun the installer."
    rendered = "\n".join(f"  {command}" for command in commands)
    return (
        "Uninstall the conflicting Pi registration with:\n"
        f"{rendered}\n"
        "Then rerun this installer."
    )


def _preflight_pi_normal(*, allow_replacement: bool = False) -> bool:
    entries = _pi_goal_plus_entries()
    root = ROOT.resolve()
    same_root = False
    conflicts: list[dict[str, Any]] = []
    for entry in entries:
        resolved = entry.get("resolved_path")
        settings_path = entry.get("settings_path")
        if allow_replacement and entry.get("removable"):
            same_root = True
        elif (entry.get("field", "packages") == "packages" and settings_path is not None
                and (allow_replacement or resolved is not None and managed_runtime.source_owner(resolved) == root)):
            same_root = True
        else:
            conflicts.append(entry)
    if conflicts:
        raise InstallError(
            "Pi already has a conflicting Goal Plus installation:\n"
            f"  - {_render_pi_entries(conflicts)}\n"
            f"{_render_pi_conflict_remediation(conflicts)}"
        )
    return same_root


def _preflight_pi_dev(*, cwd: Path | None = None) -> None:
    # The dev launcher passes --no-extensions and then explicitly loads this
    # checkout's extension.  Normal user/project registrations therefore stay
    # untouched and cannot be loaded into the dev process; treating them as an
    # install conflict would incorrectly couple --pi-dev to --pi.
    del cwd
    extension = (ROOT / "assets/pi/extensions/goal-plus.ts").resolve()
    if not extension.is_file():
        raise InstallError(f"Pi development extension is missing: {extension}")


def _git_source_state() -> dict[str, Any]:
    try:
        commit = _run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture=True,
        ).stdout.strip()
        dirty = bool(
            _run(
                ["git", "-C", str(ROOT), "status", "--porcelain"],
                capture=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return {"source_commit": None, "source_dirty": None}
    return {"source_commit": commit or None, "source_dirty": dirty}


def _validate_dev_dir(dev_dir: Path) -> dict[str, Any] | None:
    dev_dir = dev_dir.resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), ROOT.resolve()}
    if dev_dir in forbidden:
        raise InstallError("--pi-dev-dir must be a dedicated directory")
    receipt_path = dev_dir / "installation.json"
    if not dev_dir.exists():
        return None
    if not receipt_path.is_file():
        if any(dev_dir.iterdir()):
            raise InstallError(
                f"refusing to manage non-empty Pi development directory without a receipt: {dev_dir}"
            )
        return None
    receipt = _read_json(receipt_path)
    if not isinstance(receipt, dict) or receipt.get("schema") != DEV_SCHEMA:
        raise InstallError(f"unrecognized Pi development receipt: {receipt_path}")
    installed_source = Path(str(receipt.get("source_path"))).expanduser().resolve()
    if installed_source != ROOT.resolve():
        raise InstallError(
            f"Pi development directory belongs to {installed_source}, not {ROOT.resolve()}"
        )
    return receipt


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _render_set_env(dev_dir: Path) -> str:
    launcher_dir = shlex.quote(str((dev_dir / "bin").resolve()))
    root = shlex.quote(str(ROOT.resolve()))
    return f"""#!/usr/bin/env bash
# Generated by Goal Plus install.sh --pi-dev. Source this file from Bash or Zsh.
export GOAL_PLUS_PI_DEV_ROOT={root}
_goal_plus_pi_dev_bin={launcher_dir}
case ":${{PATH}}:" in
  *":${{_goal_plus_pi_dev_bin}}:"*) ;;
  *) export PATH="${{_goal_plus_pi_dev_bin}}:${{PATH}}" ;;
esac
unset _goal_plus_pi_dev_bin
"""


def _render_pi_launcher(*, dev_dir: Path, pi_executable: str) -> str:
    root = shlex.quote(str(ROOT.resolve()))
    venv = shlex.quote(str((dev_dir / "venv").resolve()))
    pi = shlex.quote(pi_executable)
    extension = shlex.quote(
        str((ROOT / "assets/pi/extensions/goal-plus.ts").resolve())
    )
    installer = shlex.quote(str(Path(__file__).resolve()))
    return f"""#!/usr/bin/env bash
set -euo pipefail

goal_plus_root={root}
goal_plus_venv={venv}
goal_plus_pi={pi}

if [[ ! -x "$goal_plus_venv/bin/python" ]]; then
  echo "Goal Plus Pi development environment is missing: $goal_plus_venv" >&2
  echo "Rerun: $goal_plus_root/install.sh --pi-dev" >&2
  exit 1
fi

module_path="$("$goal_plus_venv/bin/python" -I -c 'import goal_plus; print(goal_plus.__file__)')"
case "$module_path" in
  "$goal_plus_root"/src/goal_plus/*) ;;
  *)
    echo "Goal Plus runtime mismatch: $module_path" >&2
    echo "Expected runtime under: $goal_plus_root/src/goal_plus" >&2
    exit 1
    ;;
esac

"$goal_plus_venv/bin/python" -I {installer} --check-pi-dev-runtime --check-cwd "$PWD"

export GOAL_PLUS_PI_DEV_ROOT="$goal_plus_root"
export GOAL_PLUS_PYTHON="$goal_plus_venv/bin/python"
export PATH="$goal_plus_venv/bin:$PATH"
exec "$goal_plus_pi" --no-extensions --no-prompt-templates \
  --extension {extension} "$@"
"""


def _print_pi_dev_usage(dev_dir: Path, *, dry_run: bool) -> None:
    heading = "安装完成后执行：" if dry_run else "后续使用方式（请在当前 shell 执行）："
    print(heading)
    print(f"  source {shlex.quote(str((dev_dir / 'set_env.sh').resolve()))}")
    print("  cd <target-workspace>")
    print("  pi-goal-plus")
    print("打开新的 shell 后，需要重新执行上面的 source 命令。")


def _prepare_pi_dev(*, dev_dir: Path, pi_executable: str, dry_run: bool) -> None:
    dev_dir = dev_dir.expanduser().resolve()
    _validate_dev_dir(dev_dir)
    venv = dev_dir / "venv"
    python = _venv_python(venv)
    receipt_path = dev_dir / "installation.json"
    receipt = {
        "schema": DEV_SCHEMA,
        "mode": "pi-dev",
        "status": "preparing",
        "source_path": str(ROOT.resolve()),
        "base_python": sys.executable,
        "venv_python": str(python),
        "pi_executable": pi_executable,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **_git_source_state(),
    }
    if dry_run:
        print(f"Would prepare Pi development directory: {dev_dir}")
    else:
        dev_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(receipt_path, json.dumps(receipt, indent=2) + "\n")

    _prepare_pi_mcp_dependencies(ROOT, dry_run=dry_run)

    if not python.is_file():
        _run([sys.executable, "-I", "-m", "venv", str(venv)], dry_run=dry_run)
    _run(
        [
            str(python),
            "-I",
            "-m",
            "pip",
            "install",
            "--prefer-binary",
            "-e",
            f"{ROOT}[dev]",
        ],
        dry_run=dry_run,
    )

    if not dry_run:
        result = _run(
            [str(python), "-I", "-c", "import goal_plus; print(goal_plus.__file__)"],
            capture=True,
        )
        module_path = Path(result.stdout.strip()).resolve()
        expected_root = (ROOT / "src" / "goal_plus").resolve()
        if module_path != expected_root and expected_root not in module_path.parents:
            raise InstallError(
                f"editable runtime resolves to {module_path}, expected under {expected_root}"
            )
        _atomic_write(dev_dir / "set_env.sh", _render_set_env(dev_dir), mode=0o755)
        _atomic_write(
            dev_dir / "bin" / "pi-goal-plus",
            _render_pi_launcher(dev_dir=dev_dir, pi_executable=pi_executable),
            mode=0o755,
        )
        receipt.update(
            {
                "status": "ready",
                "module_path": str(module_path),
                "set_env": str((dev_dir / "set_env.sh").resolve()),
                "launcher": str((dev_dir / "bin" / "pi-goal-plus").resolve()),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _atomic_write(receipt_path, json.dumps(receipt, indent=2) + "\n")
        try:
            _register_pi_dev_environment(dev_dir)
        except OSError as exc:
            print(
                "warning: Pi development environment is ready but could not be "
                f"added to the local environment registry: {exc}",
                file=sys.stderr,
            )
    else:
        print(f"Would write: {dev_dir / 'set_env.sh'}")
        print(f"Would write: {dev_dir / 'bin' / 'pi-goal-plus'}")
        print(f"Would write: {receipt_path}")

    print(
        "Pi 开发环境已就绪。"
        if not dry_run
        else "Pi 开发安装计划检查通过。"
    )
    _print_pi_dev_usage(dev_dir, dry_run=dry_run)


def _codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def _codex_provider_env_vars(*, profile: str | None = None) -> tuple[str, ...]:
    """Forward only provider-declared credential names, never their values."""
    config = _load_codex_config(_codex_config(), profile=profile)
    names = {"OPENAI_API_KEY"}
    for provider in config.get("model_providers", {}).values():
        if provider.get("env_key"):
            names.add(provider["env_key"])
        names.update(provider.get("env_http_headers", {}).values())
    if any(not isinstance(name, str) or not name or "=" in name or "\0" in name for name in names):
        raise InstallError("Codex provider credential environment names are invalid")
    return tuple(sorted(names))


def _thinkthread_profile_models(
    thinkthread: str,
    *,
    profile: str,
    fs: Path,
) -> list[dict[str, str]]:
    """Return the exact models TT will permit this Profile to delegate.

    This is deliberately sourced from ThinkThread rather than from a Codex/Pi
    catalog. TT's allowlist is the actual admission boundary for Child model
    selection, so a model absent here cannot be promised by installation.
    """
    try:
        result = _run(
            [
                thinkthread, "model", "list", "--profile", profile,
                "--fs", str(fs), "--json",
            ],
            capture=True,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise InstallError(
            f"ThinkThread model inventory failed (profile={profile}): {detail.strip()}"
        ) from exc
    if not isinstance(payload, list):
        raise InstallError(
            f"ThinkThread model inventory returned a non-list (profile={profile})"
        )
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        provider, model = item.get("provider"), item.get("model")
        if not isinstance(provider, str) or not provider.strip():
            continue
        if not isinstance(model, str) or not model.strip():
            continue
        ref = f"{provider.strip()}/{model.strip()}"
        if ref in seen:
            continue
        seen.add(ref)
        models.append({"provider": provider.strip(), "model": model.strip(), "ref": ref})
    return models


def _verify_thinkthread_profile_models(
    thinkthread: str,
    *,
    native_agent: str,
    target: dict[str, str | tuple[str, ...]],
    native_executable: str | None = None,
    codex_config_dir: Path | None = None,
) -> list[dict[str, str]]:
    """Read the exact TT model admission boundary without contacting a provider."""
    temporary_root = Path(
        os.environ.get("GOAL_PLUS_TMPDIR") or Path.cwd() / ".tmp"
    ).expanduser()
    temporary_root.mkdir(parents=True, exist_ok=True)
    inventory_root = Path(
        tempfile.mkdtemp(prefix="goal-plus-tt-model-list-", dir=temporary_root)
    )
    try:
        profile = f"goal-plus-{native_agent}"
        models = _thinkthread_profile_models(
            thinkthread, profile=profile, fs=inventory_root,
        )
        print(
            f"ThinkThread model inventory is readable (profile={profile}, "
            f"delegable_models={len(models)})"
        )
        requested_provider = target.get("provider")
        requested_model = target.get("model")
        if requested_provider is not None or requested_model is not None:
            if not isinstance(requested_provider, str) or not requested_provider.strip():
                raise InstallError(
                    f"ThinkThread model admission requires provider/model; "
                    f"got provider={requested_provider!r}, model={requested_model!r}"
                )
            if not isinstance(requested_model, str) or not requested_model.strip():
                raise InstallError(
                    f"ThinkThread model admission requires provider/model; "
                    f"got provider={requested_provider!r}, model={requested_model!r}"
                )
            requested_ref = f"{requested_provider.strip()}/{requested_model.strip()}"
            if not any(item["ref"] == requested_ref for item in models):
                allowed = ", ".join(item["ref"] for item in models) or "none"
                raise InstallError(
                    f"ThinkThread model is not admitted by profile={profile}: "
                    f"requested={requested_ref}; allowed={allowed}"
                )
        return models
    finally:
        shutil.rmtree(inventory_root, ignore_errors=True)


def _verify_codex_installation(codex: str, package: Path) -> None:
    package = package.resolve()
    manifest = _read_json(package / ".codex-plugin/plugin.json")
    version = manifest["version"]
    entries = [item for item in _codex_inventory(codex)
               if item.get("pluginId") == NORMAL_CODEX_PLUGIN_ID]
    if len(entries) != 1:
        raise InstallError(f"Codex installation verification failed: expected one {NORMAL_CODEX_PLUGIN_ID}")
    item = entries[0]
    source = item.get("source")
    source_path = source.get("path") if isinstance(source, dict) else None
    if (item.get("installed") is not True or item.get("enabled") is not True
            or item.get("version") != version
            or not isinstance(source_path, str)
            or Path(source_path).expanduser().resolve() != package):
        raise InstallError(
            f"Codex installation mismatch: expected enabled {version} from {package}; "
            f"got version={item.get('version')!r}, source={source_path!r}, "
            f"installed={item.get('installed')!r}, enabled={item.get('enabled')!r}"
        )
    if _codex_marketplaces(codex).get(NORMAL_CODEX_MARKETPLACE) != package:
        raise InstallError(f"Codex marketplace mismatch: expected {package}")
    # Inventory identifies the active version, but its source is not the cache.
    cache = _codex_config().resolve() / "plugins/cache/goal-plus/goal-plus" / version
    for name in (".codex-plugin/plugin.json", ".goal-plus-runtime.json",
                 ".mcp.json", "hooks/hooks.json"):
        try:
            matches = _read_json(cache / name) == _read_json(package / name)
        except InstallError as exc:
            raise InstallError(f"Codex cache verification failed at {cache / name}: {exc}") from exc
        if not matches:
            raise InstallError(f"Codex cache mismatch: {cache / name} does not match {package / name}")
    for name in (".goal-plus-build", ".goal-plus-python"):
        try:
            matches = (cache / name).read_bytes() == (package / name).read_bytes()
        except OSError as exc:
            raise InstallError(f"Codex cache verification failed at {cache / name}: {exc}") from exc
        if not matches:
            raise InstallError(f"Codex cache mismatch: {cache / name} does not match {package / name}")


def _install_codex(*, codex: str, package: Path, dry_run: bool, reinstall: bool = False) -> None:
    managed_runtime.check_info_command()
    previous = _codex_marketplaces(codex).get(NORMAL_CODEX_MARKETPLACE)
    previously_installed = previous is not None or (not dry_run and any(
        item.get("pluginId") == NORMAL_CODEX_PLUGIN_ID for item in _codex_inventory(codex)
    ))
    changed = previous is not None and previous != package
    managed_runtime.bind("codex-pending", _codex_config(), package, dry_run=dry_run)
    try:
        if reinstall and any(item.get("pluginId") == NORMAL_CODEX_PLUGIN_ID for item in _codex_inventory(codex)):
            _run([codex, "plugin", "remove", NORMAL_CODEX_PLUGIN_ID], dry_run=dry_run)
        if changed:
            _run([codex, "plugin", "marketplace", "remove", NORMAL_CODEX_MARKETPLACE], dry_run=dry_run)
        if previous != package:
            _run([codex, "plugin", "marketplace", "add", str(package)], dry_run=dry_run)
        _run([codex, "plugin", "add", NORMAL_CODEX_PLUGIN_ID], dry_run=dry_run)
        if not dry_run:
            _verify_codex_installation(codex, package)
        info_command = managed_runtime.install_info_command(dry_run=dry_run)
    except (OSError, subprocess.CalledProcessError, RuntimeError, KeyboardInterrupt):
        # Restore the exact prior source if registration failed after preparation.
        if changed:
            current = _codex_marketplaces(codex).get(NORMAL_CODEX_MARKETPLACE)
            if current is not None:
                _run([codex, "plugin", "marketplace", "remove", NORMAL_CODEX_MARKETPLACE], dry_run=False)
            _run([codex, "plugin", "marketplace", "add", str(previous)], dry_run=False)
            _run([codex, "plugin", "add", NORMAL_CODEX_PLUGIN_ID], dry_run=False)
        elif previous is None:
            if not previously_installed and any(
                item.get("pluginId") == NORMAL_CODEX_PLUGIN_ID for item in _codex_inventory(codex)
            ):
                _run([codex, "plugin", "remove", NORMAL_CODEX_PLUGIN_ID], dry_run=False)
            current = _codex_marketplaces(codex).get(NORMAL_CODEX_MARKETPLACE)
            if current == package:
                _run([codex, "plugin", "marketplace", "remove", NORMAL_CODEX_MARKETPLACE], dry_run=False)
        elif reinstall:
            _run([codex, "plugin", "add", NORMAL_CODEX_PLUGIN_ID], dry_run=False)
        managed_runtime.unbind("codex-pending", _codex_config())
        raise
    managed_runtime.bind("codex", _codex_config(), package, dry_run=dry_run)
    managed_runtime.unbind("codex-pending", _codex_config(), dry_run=dry_run)
    print(f"Runtime information (no Codex session): {shlex.quote(str(info_command))} codex")
    print("Codex installation is ready. Start a new thread, trust /hooks, then start another thread.")


def _verify_pi_installation(pi: str, package: Path) -> None:
    entries = _pi_goal_plus_entries()
    if (len(entries) != 1 or entries[0].get("field", "packages") != "packages"
            or entries[0].get("resolved_path") != package.resolve()):
        raise InstallError(f"Pi registration mismatch: expected exactly one package from {package}")
    result = _run([_require_executable("node"), str(SCRIPT_DIR / "inspect-pi.mjs"),
                   pi, str(_pi_agent_dir()), str(Path.cwd())], capture=True)
    try:
        resources = json.loads(result.stdout)
        for field in ("extensions", "skills"):
            expected = {(package / "assets/pi/extensions/goal-plus.ts").resolve()} if field == "extensions" else set()
            actual = {Path(item["path"]).resolve() for item in resources[field]
                      if item["enabled"] and _pi_resource_is_goal_plus(Path(item["path"]))}
            if actual != expected:
                raise InstallError(f"Pi {field} mismatch: expected {expected}; got {sorted(map(str, actual))}")
    except (ValueError, KeyError, TypeError) as exc:
        raise InstallError(f"Could not decode Pi resource inventory: {exc}") from exc
    managed_runtime.verify(package)


def _prepare_pi_mcp_dependencies(package: Path, *, dry_run: bool) -> None:
    _run([
        _require_executable("npm"), "ci", "--ignore-scripts", "--no-audit", "--no-fund",
        "--prefix", str(package / "assets/pi"),
    ], dry_run=dry_run)


def _install_pi(*, pi: str, package: Path, dry_run: bool, reinstall: bool = False) -> None:
    managed_runtime.check_info_command()
    _prepare_pi_mcp_dependencies(package, dry_run=dry_run)
    registrations = _pi_goal_plus_entries()
    entries = [entry for entry in registrations if entry.get("settings_path") is not None
               and entry.get("field", "packages") == "packages"]
    resources = [entry for entry in registrations if entry.get("field") in {"extensions", "skills"}]
    settings = _pi_agent_dir() / "settings.json"
    original = settings.read_text() if settings.is_file() else None
    removed: set[str] = set()
    managed_runtime.bind("pi-pending", _pi_agent_dir(), package, dry_run=dry_run)
    try:
        _remove_pi_resource_registrations(resources, dry_run=dry_run)
        for entry in entries:
            if not reinstall and entry.get("resolved_path") == package:
                continue
            # Pi stores relative paths relative to settings.json, whereas its CLI
            # resolves local arguments against CWD.
            source = _pi_cli_source(entry)
            _run([pi, "remove", source], dry_run=dry_run)
            removed.add(entry["source"])
        if reinstall or not any(entry.get("resolved_path") == package for entry in entries):
            _run([pi, "install", str(package)], dry_run=dry_run)
        if not dry_run and original and removed:
            # Preserve package-level extension/skill filters during a local-path update.
            old_packages = json.loads(original).get("packages", [])
            current = _read_json(settings)
            if not isinstance(current, dict):
                raise InstallError(f"Pi settings must be an object after installation: {settings}")
            current["packages"] = [
                ({**entry, "source": str(package)} if isinstance(entry, dict) else str(package))
                if _package_source(entry) in removed else entry
                for entry in old_packages
            ]
            _atomic_write(settings, json.dumps(current, indent=2) + "\n")
        if not dry_run:
            _verify_pi_installation(pi, package)
        info_command = managed_runtime.install_info_command(dry_run=dry_run)
    except (OSError, subprocess.CalledProcessError, RuntimeError, KeyboardInterrupt):
        if not dry_run:
            if original is None:
                settings.unlink(missing_ok=True)
            else:
                _atomic_write(settings, original)
            managed_runtime.unbind("pi-pending", _pi_agent_dir())
        raise
    managed_runtime.bind("pi", _pi_agent_dir(), package, dry_run=dry_run)
    managed_runtime.unbind("pi-pending", _pi_agent_dir(), dry_run=dry_run)
    print(f"Runtime information (no Pi session): {shlex.quote(str(info_command))} pi")
    print("Pi installation is ready. Restart Pi before using /goal-plus.")


def _confirm_reinstall(installed: list[str], *, assume_yes: bool, dry_run: bool) -> bool:
    if not installed:
        return True
    print("Goal Plus is already installed:\n  - " + "\n  - ".join(installed))
    if dry_run:
        print("Would ask to uninstall and reinstall the selected host registrations.")
        return True
    if assume_yes:
        return True
    while True:
        try:
            answer = input("Uninstall and reinstall these registrations? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer in {"", "n", "no"}:
            print("Installation cancelled; existing registrations are unchanged.")
            return False
        if answer in {"y", "yes"}:
            return True
        print("Please enter y or n.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Goal Plus for Codex, Pi, or a checkout-local Pi development launcher."
    )
    parser.add_argument(
        "--codex", action="store_true", help="Install the normal Codex plugin."
    )
    parser.add_argument(
        "--pi", action="store_true", help="Install the normal user-scoped Pi package."
    )
    parser.add_argument("-y", "--yes", action="store_true", help="Confirm replacement of existing selected host registrations.")
    parser.add_argument(
        "--thinkthread",
        action="store_true",
        help=(
            "Install the managed goal-plus-pi/goal-plus-codex ThinkThread Profile; "
            "requires --pi or --codex."
        ),
    )
    parser.add_argument(
        "--codex-profile",
        metavar="NAME",
        help=(
            "Overlay the Codex profile NAME.config.toml from the Codex config directory "
            "when building the managed ThinkThread Codex config; requires --codex."
        ),
    )
    parser.add_argument(
        "--agent-posix-source",
        type=Path,
        help=(
            "Build the supported Agent POSIX SDK from a local sdk/agent-posix/ts "
            "source directory; requires --thinkthread."
        ),
    )
    parser.add_argument(
        "--agent-posix-package",
        type=Path,
        help="Install an offline Agent POSIX .tgz package; requires --thinkthread.",
    )
    parser.add_argument(
        "--pi-dev",
        action="store_true",
        help="Prepare an isolated editable runtime and pi-goal-plus launcher.",
    )
    parser.add_argument("--runtime-info", action="store_true", help="Show installed host build and Python identities")
    parser.add_argument(
        "--env-show",
        action="store_true",
        help="List managed Pi development environments and their disk usage.",
    )
    parser.add_argument(
        "--pi-dev-dir",
        type=Path,
        default=ROOT / ".goal-plus-dev",
        help="Managed Pi development directory (default: .goal-plus-dev in this checkout).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Check conflicts and print writes."
    )
    parser.add_argument(
        "--check-pi-dev-runtime", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--check-cwd", type=Path, default=Path.cwd(), help=argparse.SUPPRESS
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.runtime_info:
        for host, config in (("codex", _codex_config()), ("pi", _pi_agent_dir()),
                             ("thinkthread", _thinkthread_profile_path()),
                             ("thinkthread", _thinkthread_profile_path("codex"))):
            binding = managed_runtime.binding_path(host, config)
            if binding.is_file():
                package = Path(json.loads(binding.read_text())["package"])
                print(json.dumps({"host": host, **managed_runtime.verify(package)}, indent=2))
        return 0
    if args.check_pi_dev_runtime:
        _preflight_pi_dev(cwd=args.check_cwd)
        return 0
    if args.env_show:
        if args.codex or args.pi or args.pi_dev or args.thinkthread or args.dry_run:
            parser.error("--env-show cannot be combined with install or dry-run options")
        _show_pi_dev_environments(current_dev_dir=args.pi_dev_dir)
        return 0
    if not (args.codex or args.pi or args.pi_dev or args.thinkthread):
        parser.error(
            "choose --codex, --pi, --pi-dev, --thinkthread, or --env-show"
        )
    if args.pi and args.pi_dev:
        parser.error("--pi and --pi-dev are mutually exclusive")
    if args.thinkthread and not (args.pi or args.codex):
        parser.error("--thinkthread requires --pi or --codex")
    if args.codex_profile and not (args.codex and args.thinkthread):
        parser.error("--codex-profile requires --codex --thinkthread")
    if (args.agent_posix_source or args.agent_posix_package) and not args.thinkthread:
        parser.error("Agent POSIX source/package options require --thinkthread")
    if args.agent_posix_source and args.agent_posix_package:
        parser.error("choose either --agent-posix-source or --agent-posix-package")

    try:
        codex = _require_executable("codex") if args.codex else None
        pi = _require_executable("pi") if args.pi or args.pi_dev else None
        thinkthread = _require_executable("tt") if args.thinkthread else None
        node = _require_executable("node") if args.thinkthread else None
        npm = _require_executable("npm") if args.thinkthread else None
        git = (
            _require_executable("git")
            if args.thinkthread
            and args.agent_posix_source is None
            and args.agent_posix_package is None
            else None
        )
        codex_marketplace_exists = (
            _preflight_codex(codex, allow_replacement=True) if codex is not None else None
        )
        if args.pi:
            _preflight_pi_normal(allow_replacement=True)
        installed = []
        if codex is not None:
            installed.extend(f"Codex: {item.get('pluginId')} from {item.get('source')}"
                             for item in _codex_inventory(codex) if item.get("pluginId") == NORMAL_CODEX_PLUGIN_ID)
            if codex_marketplace_exists and not installed:
                installed.append(f"Codex marketplace: {_codex_marketplaces(codex)[NORMAL_CODEX_MARKETPLACE]}")
        if args.pi:
            installed.extend(f"Pi: {entry['source']}" for entry in _pi_goal_plus_entries())
        confirmed = _confirm_reinstall(
            installed, assume_yes=args.yes, dry_run=args.dry_run
        )
        if not confirmed:
            return 0
        _report_sandbox_support(codex=args.codex, pi=args.pi or args.pi_dev)
        codex_env_vars = (
            _codex_provider_env_vars(profile=args.codex_profile)
            if args.codex
            else ()
        )
        package = managed_runtime.prepare(
            ROOT, dry_run=args.dry_run,
            codex_env_vars=codex_env_vars,
        ) if args.codex or args.pi else None
        thinkthread_sdk_plan = (
            _preflight_thinkthread_sdk(
                node_executable=node,
                npm_executable=npm,
                git_executable=git,
                source=args.agent_posix_source,
                package=args.agent_posix_package,
                allow_replacement=confirmed,
            )
            if thinkthread is not None and node is not None and npm is not None
            else None
        )
        thinkthread_plan = (
            _preflight_thinkthread_profile(
                thinkthread,
                package_root=package,
                runtime_python=package.parent / "venv/bin/python",
                node_executable=node,
                sdk_entry=(
                    thinkthread_sdk_plan.entry
                    if thinkthread_sdk_plan is not None
                    else None
                ),
                allow_replacement=confirmed,
            )
            if thinkthread is not None and args.pi
            else None
        )
        thinkthread_codex_config_plan = (
            _plan_thinkthread_codex_config(
                package,
                profile=args.codex_profile,
                codex_env_vars=codex_env_vars,
            )
            if thinkthread is not None and args.codex
            else None
        )
        thinkthread_codex_plan = (
            _preflight_thinkthread_profile(
                thinkthread,
                package_root=package,
                runtime_python=package.parent / "venv/bin/python",
                node_executable=node,
                sdk_entry=(
                    thinkthread_sdk_plan.entry
                    if thinkthread_sdk_plan is not None
                    else None
                ),
                native_agent="codex",
                codex_config=thinkthread_codex_config_plan,
                allow_replacement=confirmed,
            )
            if thinkthread_codex_config_plan is not None
            else None
        )
        if args.pi_dev:
            _preflight_pi_dev()
            _validate_dev_dir(args.pi_dev_dir.expanduser().resolve())

        if (
            args.codex
            and codex is not None
            and codex_marketplace_exists is not None
        ):
            _install_codex(
                codex=codex,
                package=package,
                dry_run=args.dry_run,
                reinstall=True,
            )
        if args.pi and pi is not None:
            _install_pi(pi=pi, package=package, dry_run=args.dry_run, reinstall=True)
        if thinkthread_sdk_plan is not None:
            _install_thinkthread_sdk(
                thinkthread_sdk_plan,
                dry_run=args.dry_run,
            )
        if thinkthread_plan is not None:
            _install_thinkthread_profile(
                thinkthread_plan,
                dry_run=args.dry_run,
            )
            managed_runtime.bind("thinkthread", thinkthread_plan.path, package, dry_run=args.dry_run)
            print(f"Runtime information: {shlex.quote(str(managed_runtime.home() / 'bin/goal-plus-info'))} pi --thinkthread")
        if thinkthread_codex_config_plan is not None:
            _install_thinkthread_codex_config(
                thinkthread_codex_config_plan,
                dry_run=args.dry_run,
            )
        if thinkthread_codex_plan is not None:
            _install_thinkthread_profile(
                thinkthread_codex_plan,
                dry_run=args.dry_run,
            )
            managed_runtime.bind("thinkthread", thinkthread_codex_plan.path, package, dry_run=args.dry_run)
            print(f"Runtime information: {shlex.quote(str(managed_runtime.home() / 'bin/goal-plus-info'))} codex --thinkthread")
        if args.pi_dev and pi is not None:
            _prepare_pi_dev(
                dev_dir=args.pi_dev_dir,
                pi_executable=pi,
                dry_run=args.dry_run,
            )
        if not args.dry_run:
            # Installation proves deterministic wiring and reads the TT
            # allowlist. A live model request is deliberately left to the
            # user's actual task so installation never incurs provider cost.
            if thinkthread_codex_plan is not None:
                _verify_thinkthread_profile_models(
                    thinkthread,
                    native_agent="codex",
                    target={},
                )
            if thinkthread_plan is not None:
                _verify_thinkthread_profile_models(
                    thinkthread,
                    native_agent="pi",
                    target={},
                )
    except (RuntimeError, subprocess.CalledProcessError, OSError, KeyboardInterrupt) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
