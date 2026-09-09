#!/usr/bin/env python3
"""Uninstall Goal Plus host registrations and checkout-owned local components."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import managed_runtime
import install as install_support  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "goal-plus"
COMPONENT_CODEX = "codex"
COMPONENT_PI = "pi"
COMPONENT_PI_DEV = "pi-dev"
COMPONENT_THINKTHREAD = "thinkthread"
COMPONENT_PYTHON_RUNTIME = "runtime"
ALL_COMPONENTS = frozenset(
    {
        COMPONENT_CODEX,
        COMPONENT_PI,
        COMPONENT_PI_DEV,
        COMPONENT_THINKTHREAD,
        COMPONENT_PYTHON_RUNTIME,
    }
)


class UninstallError(RuntimeError):
    """A safe, actionable uninstallation failure."""


@dataclass(frozen=True)
class UninstallPlan:
    codex_executable: str | None
    remove_codex_plugin: bool
    remove_codex_marketplace: bool
    pi_executable: str | None
    pi_sources: tuple[str, ...]
    pi_dev_dir: Path | None
    pi_dev_registry_path: Path | None
    remove_runtime: bool
    notes: tuple[str, ...]
    goal_plus_thinkthread_profile_path: Path | None = None
    goal_plus_thinkthread_sdk_path: Path | None = None
    pi_resources: tuple[dict[str, Any], ...] = ()

    @property
    def has_changes(self) -> bool:
        return any(
            (
                self.remove_codex_plugin,
                self.remove_codex_marketplace,
                bool(self.pi_sources),
                bool(self.pi_resources),
                self.pi_dev_dir is not None,
                self.pi_dev_registry_path is not None,
                self.goal_plus_thinkthread_profile_path is not None,
                self.goal_plus_thinkthread_sdk_path is not None,
                self.remove_runtime,
            )
        )


def _run(argv: list[str], *, dry_run: bool) -> None:
    print("+", shlex.join(argv))
    if not dry_run:
        subprocess.run(argv, check=True)


def _resolved_source(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    return Path(value).expanduser().resolve()


def _codex_plan(codex: str | None) -> tuple[bool, bool, list[str]]:
    if codex is None:
        return False, False, [
            "Codex is not on PATH; no Codex registration was inspected."
        ]

    inventory = install_support._codex_inventory(codex)
    marketplaces = install_support._codex_marketplaces(codex)
    root = ROOT.resolve()
    notes: list[str] = []
    marketplace_source = marketplaces.get(install_support.NORMAL_CODEX_MARKETPLACE)
    remove_marketplace = marketplace_source is not None
    if marketplace_source is not None and marketplace_source != root:
        notes.append(
            "Warning: Codex marketplace 'goal-plus' comes from "
            f"{marketplace_source}, not the current checkout {root}."
        )

    remove_plugin = False
    for item in inventory:
        plugin_id = str(item.get("pluginId") or "")
        if plugin_id != install_support.NORMAL_CODEX_PLUGIN_ID:
            if plugin_id.startswith("goal-plus@"):
                notes.append(f"Preserved unrelated Codex plugin: {plugin_id}")
            continue
        source = item.get("source")
        source_path = source.get("path") if isinstance(source, dict) else None
        resolved = _resolved_source(source_path)
        if resolved is not None and resolved != root:
            notes.append(
                f"Warning: Codex plugin {plugin_id} comes from {resolved}, "
                f"not the current checkout {root}."
            )
        if resolved is None and not remove_marketplace:
            notes.append(
                f"Warning: Codex did not report the source of {plugin_id}."
            )
        remove_plugin = True
    return remove_plugin, remove_marketplace, notes


def _pi_sources(
    pi: str | None, *, require_executable: bool = True
) -> tuple[tuple[str, ...], list[str]]:
    root = ROOT.resolve()
    sources: list[str] = []
    notes: list[str] = []
    for entry in install_support._pi_goal_plus_entries():
        if entry.get("field") in {"extensions", "skills"}:
            if not entry.get("removable"):
                notes.append(f"Preserved shared Pi {entry['field']} path: {entry['source']}")
            continue
        if entry.get("settings_path") is None:
            notes.append(f"Preserved loose Pi resource: {entry['source']}")
            continue
        resolved = entry.get("resolved_path")
        sources.append(install_support._pi_cli_source(entry))
        if resolved != root:
            notes.append(
                f"Warning: Pi registration comes from {resolved or entry['source']}, "
                f"not the current checkout {root}."
            )
    unique_sources = tuple(dict.fromkeys(sources))
    if unique_sources and pi is None and require_executable:
        raise UninstallError(
            "Goal Plus is registered in Pi, but the pi executable is not on PATH"
        )
    return unique_sources, notes


def _registry_owned_by_current_checkout(path: Path, dev_dir: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise UninstallError(
            f"could not inspect Pi development registry {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise UninstallError(f"unrecognized Pi development registry entry: {path}")
    registered_dev = _resolved_source(payload.get("dev_dir"))
    registered_source = _resolved_source(payload.get("source_path"))
    if registered_dev != dev_dir or registered_source != ROOT.resolve():
        raise UninstallError(
            "refusing to remove Pi development registry not owned by this "
            f"checkout: {path}"
        )
    return True


def _pi_dev_plan(dev_dir: Path) -> tuple[Path | None, Path | None, list[str]]:
    dev_dir = dev_dir.expanduser().resolve()
    receipt = install_support._validate_dev_dir(dev_dir)
    notes: list[str] = []
    remove_dev_dir = dev_dir if receipt is not None else None
    if dev_dir.exists() and receipt is None:
        notes.append(f"Preserved unmanaged Pi development directory: {dev_dir}")

    registry_path = install_support._pi_dev_registry_path(dev_dir)
    remove_registry = (
        registry_path
        if _registry_owned_by_current_checkout(registry_path, dev_dir)
        else None
    )
    return remove_dev_dir, remove_registry, notes


def _goal_plus_thinkthread_profile_plan(
    *,
    require_owned: bool,
) -> tuple[Path | None, list[str]]:
    path = install_support._thinkthread_profile_path()
    if not path.exists():
        return None, [f"Goal Plus ThinkThread Profile is not installed: {path}"]
    source = install_support._managed_thinkthread_profile_source(path)
    if source == ROOT.resolve():
        return path, []
    detail = f"managed by {source}" if source is not None else "not Goal Plus-managed"
    if require_owned:
        raise UninstallError(
            "refusing to remove Goal Plus ThinkThread Profile because it is "
            f"{detail}: {path}"
        )
    return None, [f"Preserved ThinkThread Profile ({detail}): {path}"]


def _goal_plus_thinkthread_sdk_plan(
    *,
    require_owned: bool,
) -> tuple[Path | None, list[str]]:
    path = install_support._thinkthread_sdk_root()
    if not path.exists():
        return None, [f"Goal Plus Agent POSIX SDK is not installed: {path}"]
    source = install_support._managed_thinkthread_sdk_source(path)
    if source == ROOT.resolve():
        return path, []
    detail = f"managed by {source}" if source is not None else "not Goal Plus-managed"
    if require_owned:
        raise UninstallError(
            "refusing to remove Agent POSIX SDK because it is "
            f"{detail}: {path}"
        )
    return None, [f"Preserved Agent POSIX SDK ({detail}): {path}"]


def _runtime_plan() -> tuple[bool, list[str]]:
    exists = (managed_runtime.home() / "releases").is_dir()
    return exists, ["Legacy pip environments are not modified; only unreferenced managed releases are removed."]


def _build_plan(
    dev_dir: Path, *, components: frozenset[str] | None = None
) -> UninstallPlan:
    selected = ALL_COMPONENTS if components is None else components
    if not selected or not selected.issubset(ALL_COMPONENTS):
        raise UninstallError(f"invalid uninstall component selection: {sorted(selected)}")

    remove_codex = COMPONENT_CODEX in selected
    remove_pi = COMPONENT_PI in selected
    remove_pi_dev = COMPONENT_PI_DEV in selected
    remove_thinkthread = COMPONENT_THINKTHREAD in selected
    remove_python = COMPONENT_PYTHON_RUNTIME in selected

    inspect_codex = remove_codex or remove_python
    inspect_pi = remove_pi or remove_python
    codex = shutil.which("codex") if inspect_codex else None
    pi = shutil.which("pi") if inspect_pi else None

    codex_plugin = False
    codex_marketplace = False
    codex_notes: list[str] = []
    if inspect_codex:
        codex_plugin, codex_marketplace, codex_notes = _codex_plan(codex)

    pi_sources: tuple[str, ...] = ()
    pi_notes: list[str] = []
    if inspect_pi:
        pi_sources, pi_notes = _pi_sources(
            pi,
            require_executable=remove_pi,
        )
    pi_entries = install_support._pi_goal_plus_entries() if inspect_pi else []
    pi_resources = tuple(entry for entry in pi_entries if entry.get("removable"))
    retained_pi_resources = [entry for entry in pi_entries
                            if (entry.get("settings_path") is None or entry.get("field"))
                            and not entry.get("removable")]

    remove_dev_dir: Path | None = None
    remove_registry: Path | None = None
    dev_notes: list[str] = []
    if remove_pi_dev:
        remove_dev_dir, remove_registry, dev_notes = _pi_dev_plan(dev_dir)

    inspect_thinkthread = remove_thinkthread or remove_python
    owned_thinkthread_profile: Path | None = None
    owned_thinkthread_sdk: Path | None = None
    thinkthread_notes: list[str] = []
    if inspect_thinkthread:
        owned_thinkthread_profile, thinkthread_notes = (
            _goal_plus_thinkthread_profile_plan(
                require_owned=remove_thinkthread,
            )
        )
        owned_thinkthread_sdk, sdk_notes = _goal_plus_thinkthread_sdk_plan(
            require_owned=remove_thinkthread,
        )
        thinkthread_notes.extend(sdk_notes)

    remove_runtime = False
    runtime_notes: list[str] = []
    if remove_python:
        remove_runtime, runtime_notes = _runtime_plan()
        remaining_hosts: list[str] = []
        if remove_runtime and not remove_codex and (codex_plugin or codex_marketplace):
            remaining_hosts.append("Codex")
        if remove_runtime and not remove_pi and (pi_sources or pi_resources):
            remaining_hosts.append("Pi")
        if remove_runtime and retained_pi_resources:
            remaining_hosts.append("Pi resources requiring explicit removal")
        if (
            remove_runtime
            and not remove_thinkthread
            and owned_thinkthread_profile is not None
        ):
            remaining_hosts.append("Goal Plus ThinkThread Profile")
        if remaining_hosts:
            rendered = " and ".join(remaining_hosts)
            raise UninstallError(
                "refusing to remove the Python runtime while an unselected "
                f"{rendered} registration remains; remove that host first or omit "
                "--runtime"
            )

    return UninstallPlan(
        codex_executable=codex if remove_codex else None,
        remove_codex_plugin=remove_codex and codex_plugin,
        remove_codex_marketplace=remove_codex and codex_marketplace,
        pi_executable=pi if remove_pi else None,
        pi_sources=pi_sources if remove_pi else (),
        pi_resources=pi_resources if remove_pi else (),
        pi_dev_dir=remove_dev_dir,
        pi_dev_registry_path=remove_registry,
        remove_runtime=remove_runtime,
        notes=tuple(
            (*codex_notes, *pi_notes, *dev_notes, *thinkthread_notes, *runtime_notes)
        ),
        goal_plus_thinkthread_profile_path=(
            owned_thinkthread_profile if remove_thinkthread else None
        ),
        goal_plus_thinkthread_sdk_path=(
            owned_thinkthread_sdk if remove_thinkthread else None
        ),
    )


def _print_plan(plan: UninstallPlan, *, dry_run: bool) -> None:
    heading = "Goal Plus uninstall dry run:" if dry_run else "Goal Plus uninstall plan:"
    print(heading)
    if plan.remove_codex_plugin:
        print(f"- Codex plugin: {install_support.NORMAL_CODEX_PLUGIN_ID}")
    if plan.remove_codex_marketplace:
        print(f"- Codex marketplace: {install_support.NORMAL_CODEX_MARKETPLACE}")
    for source in plan.pi_sources:
        print(f"- Pi registration: {source}")
    for entry in plan.pi_resources:
        print(f"- Pi {entry['field']} registration: {entry['source']}")
    if plan.pi_dev_dir is not None:
        print(f"- Pi development environment: {plan.pi_dev_dir}")
    if plan.pi_dev_registry_path is not None:
        print(f"- Pi development registry: {plan.pi_dev_registry_path}")
    if plan.goal_plus_thinkthread_profile_path is not None:
        print(
            "- Goal Plus ThinkThread Profile: "
            f"{plan.goal_plus_thinkthread_profile_path}"
        )
    if plan.goal_plus_thinkthread_sdk_path is not None:
        print(f"- Goal Plus Agent POSIX SDK: {plan.goal_plus_thinkthread_sdk_path}")
    if plan.remove_runtime:
        print(f"- Unreferenced managed releases: {managed_runtime.home()}")
    for note in plan.notes:
        print(f"- {note}")
    print("- Preserved: target workspace .gp records and promoted source changes", flush=True)


def _execute(plan: UninstallPlan, *, dry_run: bool) -> None:
    if plan.remove_codex_plugin:
        assert plan.codex_executable is not None
        _run(
            [
                plan.codex_executable,
                "plugin",
                "remove",
                install_support.NORMAL_CODEX_PLUGIN_ID,
            ],
            dry_run=dry_run,
        )
    if plan.remove_codex_marketplace:
        assert plan.codex_executable is not None
        _run(
            [
                plan.codex_executable,
                "plugin",
                "marketplace",
                "remove",
                install_support.NORMAL_CODEX_MARKETPLACE,
            ],
            dry_run=dry_run,
        )
    for source in plan.pi_sources:
        assert plan.pi_executable is not None
        _run([plan.pi_executable, "remove", source], dry_run=dry_run)
    install_support._remove_pi_resource_registrations(list(plan.pi_resources), dry_run=dry_run)

    if plan.goal_plus_thinkthread_profile_path is not None:
        if dry_run:
            print(
                "+ remove Goal Plus ThinkThread Profile",
                plan.goal_plus_thinkthread_profile_path,
            )
        else:
            source = install_support._managed_thinkthread_profile_source(
                plan.goal_plus_thinkthread_profile_path
            )
            if source != ROOT.resolve():
                raise UninstallError(
                    "Goal Plus ThinkThread Profile ownership changed before removal: "
                    f"{plan.goal_plus_thinkthread_profile_path}"
                )
            plan.goal_plus_thinkthread_profile_path.unlink()

    if plan.goal_plus_thinkthread_sdk_path is not None:
        if dry_run:
            print("+ remove managed Agent POSIX SDK", plan.goal_plus_thinkthread_sdk_path)
        else:
            source = install_support._managed_thinkthread_sdk_source(
                plan.goal_plus_thinkthread_sdk_path
            )
            if source != ROOT.resolve():
                raise UninstallError(
                    "Agent POSIX SDK ownership changed before removal: "
                    f"{plan.goal_plus_thinkthread_sdk_path}"
                )
            shutil.rmtree(plan.goal_plus_thinkthread_sdk_path)

    if plan.pi_dev_dir is not None:
        if dry_run:
            print("+ remove managed directory", plan.pi_dev_dir)
        else:
            receipt = install_support._validate_dev_dir(plan.pi_dev_dir)
            if receipt is None:
                raise UninstallError(
                    "Pi development receipt disappeared before removal: "
                    f"{plan.pi_dev_dir}"
                )
            shutil.rmtree(plan.pi_dev_dir)
    if plan.pi_dev_registry_path is not None:
        if dry_run:
            print("+ remove registry", plan.pi_dev_registry_path)
        else:
            plan.pi_dev_registry_path.unlink(missing_ok=True)
    if plan.remove_codex_marketplace or plan.remove_codex_plugin:
        managed_runtime.unbind("codex", install_support._codex_config(), dry_run=dry_run)
    if plan.pi_sources or plan.pi_resources:
        remaining = install_support._pi_goal_plus_entries() if not dry_run else []
        if any(entry.get("field") or entry.get("settings_path") is None for entry in remaining):
            print("Preserved Pi runtime binding: loose or shared Goal Plus resources remain.")
        else:
            managed_runtime.unbind("pi", install_support._pi_agent_dir(), dry_run=dry_run)
    if plan.goal_plus_thinkthread_profile_path is not None:
        managed_runtime.unbind("thinkthread", plan.goal_plus_thinkthread_profile_path, dry_run=dry_run)
    if plan.remove_runtime:
        managed_runtime.cleanup(dry_run=dry_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Uninstall Goal Plus host registrations and checkout-owned local "
            "components. With no component selector, remove all removable components."
        )
    )
    parser.add_argument(
        "--codex",
        action="store_true",
        help="Remove the normal Goal Plus Codex registration regardless of source.",
    )
    parser.add_argument(
        "--pi",
        action="store_true",
        help="Remove normal Goal Plus Pi registrations regardless of source.",
    )
    parser.add_argument(
        "--pi-dev",
        action="store_true",
        help="Remove this checkout's managed Pi development environment.",
    )
    parser.add_argument(
        "--thinkthread",
        action="store_true",
        help=(
            "Remove only this checkout's goal-plus-pi Profile; never remove "
            "ThinkThread Runtime, Roots, or state."
        ),
    )
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="Remove unreferenced managed releases (never pip environments or ThinkThread).",
    )
    parser.add_argument(
        "--pi-dev-dir",
        type=Path,
        default=ROOT / ".goal-plus-dev",
        help=(
            "Managed Pi development directory "
            "(default: .goal-plus-dev in this checkout)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect the removal plan without changes.",
    )
    return parser


def _selected_components(args: argparse.Namespace) -> frozenset[str]:
    selected = frozenset(
        component
        for component, enabled in (
            (COMPONENT_CODEX, args.codex),
            (COMPONENT_PI, args.pi),
            (COMPONENT_PI_DEV, args.pi_dev),
            (COMPONENT_THINKTHREAD, args.thinkthread),
            (COMPONENT_PYTHON_RUNTIME, args.runtime),
        )
        if enabled
    )
    return selected or ALL_COMPONENTS


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = _build_plan(
            args.pi_dev_dir,
            components=_selected_components(args),
        )
        _print_plan(plan, dry_run=args.dry_run)
        if plan.has_changes:
            _execute(plan, dry_run=args.dry_run)
        else:
            print("No removable Goal Plus components were found.")
    except (
        UninstallError,
        install_support.InstallError,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
