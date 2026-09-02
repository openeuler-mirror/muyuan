#!/usr/bin/env python3
"""Install Goal Plus for Codex, Pi, or an isolated Pi development checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEV_SCHEMA = "goal-plus.pi-dev-installation.v1"
DEV_REGISTRY_SCHEMA = "goal-plus.pi-dev-registry-entry.v1"
NORMAL_CODEX_PLUGIN_ID = "goal-plus@goal-plus"
NORMAL_CODEX_MARKETPLACE = "goal-plus"
NORMAL_RUNTIME_COMMANDS = (
    "goal-plus",
    "goal-plus-codex-doctor",
    "goal-plus-pi-tool",
    "goal-plus-pi-worker",
    "goal-plus-pi-pool",
)


class InstallError(RuntimeError):
    """A safe, actionable installation failure."""


def _run(
    argv: list[str],
    *,
    capture: bool = False,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
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
    )


def _require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise InstallError(f"required executable is not on PATH: {name}")
    return str(Path(executable).resolve())


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


def _preflight_codex(codex: str) -> tuple[bool, bool]:
    root = ROOT.resolve()
    normal_installed = False
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
        elif resolved_source is not None and resolved_source != root:
            conflicts.append(
                f"{NORMAL_CODEX_PLUGIN_ID!r} points to {resolved_source}, expected {root}"
            )
        else:
            normal_installed = True

    marketplaces = _codex_marketplaces(codex)
    marketplace_root = marketplaces.get(NORMAL_CODEX_MARKETPLACE)
    if marketplace_root is not None and marketplace_root != root:
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
    return normal_installed, marketplace_root is not None


def _pi_agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path.home() / ".pi" / "agent"
    )


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
        candidate = settings_path.parent / candidate
    return candidate.resolve()


def _source_is_goal_plus(source: str, settings_path: Path) -> tuple[bool, Path | None]:
    local_path = _local_source_path(source, settings_path)
    if local_path is not None:
        manifest = local_path / "package.json"
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
    packages = payload.get("packages", []) if isinstance(payload, dict) else []
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


def _pi_goal_plus_entries(*, cwd: Path | None = None) -> list[dict[str, Any]]:
    agent_dir = _pi_agent_dir()
    matches = _settings_goal_plus_entries(agent_dir / "settings.json", scope="user")
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
    return matches


def _render_pi_entries(entries: list[dict[str, Any]]) -> str:
    lines = []
    for entry in entries:
        resolved = entry.get("resolved_path")
        suffix = f" -> {resolved}" if resolved else ""
        lines.append(f"[{entry['scope']}] {entry['source']}{suffix}")
    return "\n  - ".join(lines)


def _preflight_pi_normal() -> bool:
    entries = _pi_goal_plus_entries()
    root = ROOT.resolve()
    same_root = False
    conflicts: list[dict[str, Any]] = []
    for entry in entries:
        resolved = entry.get("resolved_path")
        settings_path = entry.get("settings_path")
        if settings_path is not None and resolved == root:
            same_root = True
        else:
            conflicts.append(entry)
    if conflicts:
        raise InstallError(
            "Pi already has a conflicting Goal Plus installation:\n"
            f"  - {_render_pi_entries(conflicts)}\n"
            "Remove or disable those Goal Plus resources explicitly, then rerun the installer."
        )
    return same_root


def _preflight_pi_dev(*, cwd: Path | None = None) -> None:
    entries = _pi_goal_plus_entries(cwd=cwd)
    root = ROOT.resolve()
    allowed_paths = {
        root,
        (root / ".pi" / "extensions" / "goal-plus.ts").resolve(),
        (root / ".pi" / "skills" / "goal-plus").resolve(),
    }
    conflicts = [
        entry
        for entry in entries
        if entry.get("resolved_path") not in allowed_paths
    ]
    if conflicts:
        raise InstallError(
            "Pi development mode found another active Goal Plus source; "
            "pi-goal-plus already loads this checkout explicitly:\n"
            f"  - {_render_pi_entries(conflicts)}\n"
            "Remove or disable those Goal Plus resources explicitly. Other Pi packages and "
            "authentication remain untouched."
        )


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


def _preflight_normal_runtime() -> Path:
    scripts_dir = Path(sysconfig.get_path("scripts")).expanduser().resolve()
    path_dirs = {
        Path(item).expanduser().resolve()
        for item in os.environ.get("PATH", "").split(os.pathsep)
        if item
    }
    if scripts_dir not in path_dirs:
        raise InstallError(
            f"the selected Python scripts directory is not on PATH: {scripts_dir}\n"
            f"Activate the environment for {sys.executable}, then rerun install.sh."
        )

    conflicts: list[str] = []
    for command in NORMAL_RUNTIME_COMMANDS:
        executable = shutil.which(command)
        if executable is None:
            continue
        resolved = Path(executable).resolve()
        if resolved.parent != scripts_dir:
            conflicts.append(f"{command}: {resolved}")
    if conflicts:
        rendered = "\n  - ".join(conflicts)
        raise InstallError(
            "PATH already resolves Goal Plus commands from another Python environment:\n"
            f"  - {rendered}\n"
            f"Selected Python: {sys.executable}\n"
            "Activate the intended environment so its bin directory wins on PATH, then rerun."
        )
    return scripts_dir


def _verify_normal_runtime(scripts_dir: Path) -> None:
    failures: list[str] = []
    for command in NORMAL_RUNTIME_COMMANDS:
        expected = (scripts_dir / command).resolve()
        executable = shutil.which(command)
        resolved = Path(executable).resolve() if executable is not None else None
        if not expected.is_file() or resolved != expected:
            failures.append(
                f"{command}: resolved={resolved or 'missing'}, expected={expected}"
            )
    if failures:
        rendered = "\n  - ".join(failures)
        raise InstallError(
            "the installed Goal Plus commands are not active on PATH:\n"
            f"  - {rendered}"
        )


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
        str((ROOT / ".pi" / "extensions" / "goal-plus.ts").resolve())
    )
    skill = shlex.quote(
        str((ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").resolve())
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

module_path="$("$goal_plus_venv/bin/python" -c 'import goal_plus; print(goal_plus.__file__)')"
case "$module_path" in
  "$goal_plus_root"/src/goal_plus/*) ;;
  *)
    echo "Goal Plus runtime mismatch: $module_path" >&2
    echo "Expected runtime under: $goal_plus_root/src/goal_plus" >&2
    exit 1
    ;;
esac

"$goal_plus_venv/bin/python" {installer} --check-pi-dev-runtime --check-cwd "$PWD"

export GOAL_PLUS_PI_DEV_ROOT="$goal_plus_root"
export PATH="$goal_plus_venv/bin:$PATH"
exec "$goal_plus_pi" --no-extensions --no-prompt-templates \
  --extension {extension} --skill {skill} "$@"
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

    if not python.is_file():
        _run([sys.executable, "-m", "venv", str(venv)], dry_run=dry_run)
    _run(
        [str(python), "-m", "pip", "install", "-e", f"{ROOT}[dev]"],
        dry_run=dry_run,
    )

    if not dry_run:
        result = _run(
            [str(python), "-c", "import goal_plus; print(goal_plus.__file__)"],
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


def _install_normal_runtime(*, scripts_dir: Path, dry_run: bool) -> None:
    _run([sys.executable, "-m", "pip", "install", str(ROOT)], dry_run=dry_run)
    if not dry_run:
        _verify_normal_runtime(scripts_dir)


def _install_codex(
    *, codex: str, normal_installed: bool, marketplace_exists: bool, dry_run: bool
) -> None:
    if not marketplace_exists:
        _run(
            [codex, "plugin", "marketplace", "add", str(ROOT.resolve())],
            dry_run=dry_run,
        )
    if normal_installed:
        print(f"Codex plugin already installed: {NORMAL_CODEX_PLUGIN_ID}")
    else:
        _run([codex, "plugin", "add", NORMAL_CODEX_PLUGIN_ID], dry_run=dry_run)
    print(
        "Codex installation is ready. Start a new thread, trust /hooks, then start another thread."
    )


def _install_pi(*, pi: str, already_installed: bool, dry_run: bool) -> None:
    if already_installed:
        print(f"Pi package already points to: {ROOT.resolve()}")
    else:
        _run([pi, "install", str(ROOT.resolve())], dry_run=dry_run)
    print("Pi installation is ready. Restart Pi before using /goal-plus.")


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
    parser.add_argument(
        "--pi-dev",
        action="store_true",
        help="Prepare an isolated editable runtime and pi-goal-plus launcher.",
    )
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
    if args.check_pi_dev_runtime:
        _preflight_pi_dev(cwd=args.check_cwd)
        return 0
    if args.env_show:
        if args.codex or args.pi or args.pi_dev or args.dry_run:
            parser.error("--env-show cannot be combined with install or dry-run options")
        _show_pi_dev_environments(current_dev_dir=args.pi_dev_dir)
        return 0
    if not (args.codex or args.pi or args.pi_dev):
        parser.error("choose --codex, --pi, --pi-dev, or --env-show")
    if args.pi and args.pi_dev:
        parser.error("--pi and --pi-dev are mutually exclusive")

    try:
        codex = _require_executable("codex") if args.codex else None
        pi = _require_executable("pi") if args.pi or args.pi_dev else None
        codex_state = _preflight_codex(codex) if codex is not None else None
        pi_installed = _preflight_pi_normal() if args.pi else False
        runtime_scripts = _preflight_normal_runtime() if args.codex or args.pi else None
        if args.pi_dev:
            _preflight_pi_dev()
            _validate_dev_dir(args.pi_dev_dir.expanduser().resolve())

        if runtime_scripts is not None:
            _install_normal_runtime(
                scripts_dir=runtime_scripts,
                dry_run=args.dry_run,
            )
        if args.codex and codex is not None and codex_state is not None:
            _install_codex(
                codex=codex,
                normal_installed=codex_state[0],
                marketplace_exists=codex_state[1],
                dry_run=args.dry_run,
            )
        if args.pi and pi is not None:
            _install_pi(pi=pi, already_installed=pi_installed, dry_run=args.dry_run)
        if args.pi_dev and pi is not None:
            _prepare_pi_dev(
                dev_dir=args.pi_dev_dir,
                pi_executable=pi,
                dry_run=args.dry_run,
            )
    except (InstallError, subprocess.CalledProcessError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
