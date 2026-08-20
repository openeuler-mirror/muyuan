#!/usr/bin/env python3
"""Install, refresh, or uninstall the checkout-local Goal Plus Codex plugin."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE_NAME = "goal-plus-local"
PLUGIN_NAME = "goal-plus"
PLUGIN_ASSETS = (
    ".codex-plugin",
    ".mcp.json",
    "hooks",
    "skills",
)


def _run(argv: list[str], *, codex_home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    print("+", " ".join(argv))
    return subprocess.run(argv, check=True, text=True, env=env)


def _configured_marketplace_root(output: str) -> Path | None:
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith(f"{MARKETPLACE_NAME} "):
            continue
        return Path(line[len(MARKETPLACE_NAME) :].strip()).expanduser().resolve()
    return None


def _is_managed_marketplace(marketplace_root: Path) -> bool:
    metadata = marketplace_root / ".agents" / "plugins" / "marketplace.json"
    if not metadata.is_file():
        return False
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if payload.get("name") != MARKETPLACE_NAME:
        return False
    plugins = payload.get("plugins")
    return bool(
        isinstance(plugins, list)
        and any(
            isinstance(item, dict)
            and item.get("name") == PLUGIN_NAME
            and item.get("source")
            == {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"}
            for item in plugins
        )
    )


def _validate_marketplace_root(codex_home: Path, marketplace_root: Path) -> None:
    forbidden_roots = {Path("/").resolve(), Path.home().resolve(), codex_home.resolve()}
    if marketplace_root.resolve() in forbidden_roots:
        raise RuntimeError(
            "--marketplace-root must be a dedicated subdirectory, not /, HOME, or CODEX_HOME"
        )


def _plugin_is_installed(codex: str, *, codex_home: Path) -> bool:
    result = subprocess.run(
        [codex, "plugin", "list", "--json"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )
    try:
        payload = json.loads(result.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex returned invalid plugin inventory JSON") from exc
    installed = payload.get("installed") if isinstance(payload, dict) else None
    return bool(
        isinstance(installed, list)
        and any(
            isinstance(item, dict)
            and item.get("pluginId") == f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
            for item in installed
        )
    )


def _stage_plugin(marketplace_root: Path) -> Path:
    plugins_root = marketplace_root / "plugins"
    plugins_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".goal-plus-stage-", dir=plugins_root))
    try:
        for relative in PLUGIN_ASSETS:
            source = ROOT / relative
            destination = temporary / relative
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        manifest_path = temporary / ".codex-plugin" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        base_version = str(manifest["version"]).split("+", 1)[0]
        cachebuster = datetime.now(timezone.utc).strftime("local-%Y%m%d-%H%M%S")
        manifest["version"] = f"{base_version}+codex.{cachebuster}"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        destination = plugins_root / PLUGIN_NAME
        if destination.exists():
            if (
                destination.resolve().parent != plugins_root.resolve()
                or not _is_managed_marketplace(marketplace_root)
            ):
                raise RuntimeError(
                    f"refusing to replace an unmanaged plugin path: {destination}"
                )
            shutil.rmtree(destination)
        temporary.replace(destination)
        return destination
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _write_marketplace(marketplace_root: Path) -> Path:
    metadata_root = marketplace_root / ".agents" / "plugins"
    metadata_root.mkdir(parents=True, exist_ok=True)
    path = metadata_root / "marketplace.json"
    payload = {
        "name": MARKETPLACE_NAME,
        "interface": {"displayName": "Goal Plus Local"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Developer Tools",
            }
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def install(*, codex_home: Path, marketplace_root: Path, install_runtime: bool) -> None:
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("the `codex` executable is not on PATH")
    _validate_marketplace_root(codex_home, marketplace_root)
    if install_runtime:
        _run(
            [sys.executable, "-m", "pip", "install", "-e", str(ROOT)],
            codex_home=codex_home,
        )
    staged_plugin = _stage_plugin(marketplace_root)
    marketplace_path = _write_marketplace(marketplace_root)
    list_result = subprocess.run(
        [codex, "plugin", "marketplace", "list"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )
    configured_root = _configured_marketplace_root(list_result.stdout)
    if configured_root is None:
        _run(
            [codex, "plugin", "marketplace", "add", str(marketplace_root)],
            codex_home=codex_home,
        )
    elif configured_root != marketplace_root.resolve():
        raise RuntimeError(
            f"marketplace {MARKETPLACE_NAME!r} already points to {configured_root}; "
            "choose another --marketplace-root or remove the stale marketplace explicitly"
        )
    _run(
        [codex, "plugin", "add", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"],
        codex_home=codex_home,
    )
    print(f"Staged plugin: {staged_plugin}")
    print(f"Marketplace: {marketplace_path}")
    print("Start a new Codex thread, review/trust Goal Plus in /hooks, then start a fresh thread.")


def uninstall(
    *,
    codex_home: Path,
    marketplace_root: Path,
    uninstall_runtime: bool,
) -> None:
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("the `codex` executable is not on PATH")
    _validate_marketplace_root(codex_home, marketplace_root)
    if marketplace_root.exists() and not _is_managed_marketplace(marketplace_root):
        raise RuntimeError(
            f"refusing to remove an unmanaged marketplace path: {marketplace_root}"
        )

    list_result = subprocess.run(
        [codex, "plugin", "marketplace", "list"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )
    configured_root = _configured_marketplace_root(list_result.stdout)
    if configured_root is not None and configured_root != marketplace_root.resolve():
        raise RuntimeError(
            f"marketplace {MARKETPLACE_NAME!r} points to {configured_root}; "
            "refusing to remove a different installation"
        )
    if _plugin_is_installed(codex, codex_home=codex_home):
        _run(
            [codex, "plugin", "remove", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"],
            codex_home=codex_home,
        )
    if configured_root is not None:
        _run(
            [codex, "plugin", "marketplace", "remove", MARKETPLACE_NAME],
            codex_home=codex_home,
        )
    if marketplace_root.exists():
        shutil.rmtree(marketplace_root)
        print(f"Removed managed marketplace: {marketplace_root}")
    if uninstall_runtime:
        _run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "goal-plus"],
            codex_home=codex_home,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage Goal Plus from this checkout in a local Codex marketplace."
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
    )
    parser.add_argument(
        "--marketplace-root",
        type=Path,
        default=None,
        help="Persistent local marketplace directory (default: CODEX_HOME/local-marketplaces/goal-plus).",
    )
    parser.add_argument(
        "--skip-runtime-install",
        action="store_true",
        help="Do not run pip install -e for the goal-plus command.",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the managed plugin, marketplace, staging directory, and Python runtime.",
    )
    parser.add_argument(
        "--keep-runtime",
        action="store_true",
        help="With --uninstall, keep the shared goal-plus Python runtime installed.",
    )
    args = parser.parse_args()
    if args.uninstall and args.skip_runtime_install:
        parser.error("--skip-runtime-install is only valid when installing")
    if args.keep_runtime and not args.uninstall:
        parser.error("--keep-runtime requires --uninstall")
    codex_home = args.codex_home.expanduser().resolve()
    marketplace_root = (
        args.marketplace_root or codex_home / "local-marketplaces" / "goal-plus"
    ).expanduser().resolve()
    if args.uninstall:
        uninstall(
            codex_home=codex_home,
            marketplace_root=marketplace_root,
            uninstall_runtime=not args.keep_runtime,
        )
    else:
        install(
            codex_home=codex_home,
            marketplace_root=marketplace_root,
            install_runtime=not args.skip_runtime_install,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
