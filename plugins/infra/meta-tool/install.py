#!/usr/bin/env python3
"""Install project-local meta-tool for Pi and/or Codex, without global settings."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
FORMAT = "bate-host-install-v1"


def destination(project: Path, relative: str) -> Path:
    path = project / relative
    for part in (path, *path.parents):
        if part == project:
            break
        if part.is_symlink():
            raise ValueError(f"Install destination uses a symlink: {relative}")
    return path


def read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_object(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".meta-tool-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def pi_configuration(project: Path, runtime: Path) -> tuple[dict, dict]:
    """Validate before installing dependencies; preserve all unrelated settings."""
    package = runtime / "integrations/pi-meta-tool"
    settings = read_object(destination(project, ".pi/settings.json"))
    config = read_object(destination(project, ".pi/meta-tool/config.json"))
    packages = settings.get("packages", [])
    extensions = settings.get("extensions", [])
    if not isinstance(packages, list) or not isinstance(extensions, list):
        raise ValueError("Pi packages and extensions must be lists")
    registered = []
    for entry in packages:
        source = entry if isinstance(entry, str) else entry.get("source") if isinstance(entry, dict) else None
        if not isinstance(source, str):
            raise ValueError("Invalid Pi package entry")
        # Pi resolves project package paths relative to .pi/settings.json.
        path = Path(source).expanduser()
        path = path if path.is_absolute() else project / ".pi" / path
        if path.resolve() == package:
            if isinstance(entry, dict):
                raise ValueError("Managed Pi package has custom resource filters; preserve or remove it manually")
            registered.append(entry)
        elif path.is_dir() and read_object(path / "package.json").get("name") == "@meta-tool/pi-bate-online":
            raise ValueError("Another meta-tool Pi package is already registered; remove that entry first")
    for entry in extensions:
        if isinstance(entry, str) and not entry.startswith("!") and Path(entry).name == "meta-tool.ts":
            raise ValueError("A meta-tool extension is already explicitly registered; remove that entry first")
    settings["packages"] = [p for p in packages if p not in registered] + [str(package)]
    managed = {"metaToolRoot": str(runtime), "pythonCommand": str(runtime / ".venv/bin/python"),
               "bridgePath": str(package / "python/bridge.py")}
    for key, value in managed.items():
        if key in config and config[key] != value:
            raise ValueError(f"Existing Pi {key} differs; preserve or remove that setting before installing")
    config.update(managed)
    return settings, config


def deploy_pi(runtime: Path) -> None:
    """Stage package files and locked dependencies before replacing our adapter."""
    source = SOURCE / "integrations/pi-meta-tool"
    target = runtime / "integrations/pi-meta-tool"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pi-install-", dir=runtime) as temporary:
        stage = Path(temporary) / "package"
        stage.mkdir()
        for name in ["package.json", "package-lock.json", *read_object(source / "package.json")["files"]]:
            origin, output = source / name, stage / name
            output.parent.mkdir(parents=True, exist_ok=True)
            if origin.is_dir():
                shutil.copytree(origin, output, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(origin, output)
        subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=stage, check=True)
        previous = Path(temporary) / "previous"
        if target.exists():
            target.rename(previous)
        try:
            stage.rename(target)
        except OSError:
            if previous.exists():
                previous.rename(target)
            raise


def install(args: argparse.Namespace) -> dict:
    project = args.project.expanduser().resolve(strict=True)
    if not project.is_dir():
        raise ValueError("Project must be an existing directory")
    runtime = destination(project, ".meta-tool")
    marker = destination(project, ".meta-tool/install.json")
    for relative in (".meta-tool/.venv", ".meta-tool/.gitignore", ".meta-tool/integrations/pi-meta-tool"):
        destination(project, relative)
    if runtime.exists() and read_object(marker).get("format") != FORMAT:
        raise ValueError(".meta-tool exists but is not owned by this installer")
    pi_values = pi_configuration(project, runtime) if args.pi else None
    if args.codex:
        for path in (".bate-codex", ".agents/plugins/marketplace.json", ".codex/config.toml"):
            destination(project, path)
    config = args.config.expanduser().resolve(strict=True) if args.config else None
    traces = [str(path.expanduser().resolve()) for path in args.trace_file]
    if len(set(traces)) > 64:
        raise ValueError("At most 64 explicit trace files are supported")
    targets = [name for name in ("pi", "codex") if getattr(args, name)]
    report = {"project": str(project), "targets": targets, "runtime": str(runtime),
              "dry_run": args.dry_run, "global_settings_changed": False}
    if args.dry_run:
        return report
    if args.pi:
        if not shutil.which("node") or not shutil.which("npm"):
            raise ValueError("Pi installation requires Node.js 22.18+ and npm on PATH")
        version = subprocess.check_output(["node", "--version"], text=True).strip().lstrip("v")
        if tuple(int(p) for p in version.split(".")[:2]) < (22, 18):
            raise ValueError("Pi installation requires Node.js 22.18+")
    runtime.mkdir(mode=0o700, exist_ok=True)
    write_object(marker, {"format": FORMAT})
    (runtime / ".gitignore").write_text("*\n")
    env = runtime / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
    python = str(env / "bin/python")
    # Retain the MCP extra when refreshing Pi in a project that also uses Codex.
    extra = "[codex]" if args.codex or (project / ".bate-codex/install.json").exists() else ""
    subprocess.run([python, "-m", "pip", "install", str(SOURCE) + extra], check=True)
    if args.pi:
        deploy_pi(runtime)
    if args.codex:
        command = [python, "-m", "bate.codex", "install", "--project", str(project)]
        if config:
            command.extend(["--config", str(config)])
        for path in traces:
            command.extend(["--trace-file", path])
        subprocess.run(command, check=True)
    if pi_values:
        # Re-read to preserve unrelated settings changed while pip/npm were running.
        settings, pi_config = pi_configuration(project, runtime)
        write_object(project / ".pi/meta-tool/config.json", pi_config)
        write_object(project / ".pi/settings.json", settings)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi", action="store_true", help="Install Pi package (default if no host selected)")
    parser.add_argument("--codex", action="store_true", help="Install Codex Skill + MCP plugin")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Target project (default: current directory)")
    parser.add_argument("--config", type=Path, help="Codex only: existing BATE MCP configuration")
    parser.add_argument("--trace-file", type=Path, action="append", default=[], help="Codex only: explicit Online input; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="Show targets without installing dependencies or writing files")
    args = parser.parse_args(argv)
    if not args.pi and not args.codex:
        args.pi = True
    if (args.config or args.trace_file) and not args.codex:
        parser.error("--config and --trace-file require --codex; Pi records its own tool events")
    if sys.version_info < (3, 11) or os.name != "posix":
        parser.error("Use Python 3.11+ on Linux/macOS")
    try:
        print(json.dumps(install(args), ensure_ascii=False, indent=2))
        if not args.dry_run:
            print("Open a new trusted project session. Pi: /meta-tool; Codex: select meta-tool from / or use $meta-tool on.")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"meta-tool install: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
