"""Host-independent installation of immutable Goal Plus Python releases.

This module is shared by the source installer and uninstaller. It never imports
the application or the Python package installed in the invoking shell.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess

SCHEMA = "goal-plus.managed-runtime.v1"
PYTHON_VERSION = "3.12"
PACKAGE_PATHS = (
    "src", "scripts", "hooks", "skills", ".codex/skills", ".codex/agents",
    ".codex/hooks.example.json", ".codex/config.example.toml", ".codex-plugin",
    ".pi/extensions", ".pi/skills", ".pi/prompts", ".mcp.json", "package.json", "pyproject.toml",
    "uv.lock", "README.md", "README_zh.md", "SPEC.md", "AGENTS.md", "docs", "examples",
    "LICENSE", "install.sh", "uninstall.sh",
)


def home() -> Path:
    return Path(os.environ.get("GOAL_PLUS_INSTALL_HOME", "~/.local/share/goal-plus")).expanduser().resolve()


def files(source: Path):
    included = None
    if any((parent / ".git").exists() for parent in (source, *source.parents)):
        result = subprocess.run(["git", "-C", str(source), "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."],
                                capture_output=True, text=True, check=True)
        included = set(result.stdout.split("\0"))
    for name in PACKAGE_PATHS:
        path = source / name
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for item in candidates:
            relative = item.relative_to(source)
            if included is not None and relative.as_posix() not in included:
                continue
            if item.name == ".env" or item.name.startswith(".env."):
                continue
            if item.is_file() and not any(part in {"__pycache__", "node_modules", ".venv", ".gp", ".git", ".pytest_cache"} for part in item.parts):
                yield item.relative_to(source), item.read_bytes()


def fingerprint(source: Path, captured: list[tuple[Path, bytes]] | None = None) -> str:
    digest = hashlib.sha256((SCHEMA + PYTHON_VERSION + str(source.resolve())).encode())
    for relative, content in (files(source) if captured is None else captured):
        digest.update(str(relative).encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def read_receipt(package: Path) -> dict | None:
    path = package / ".goal-plus-runtime.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    if value.get("schema") != SCHEMA:
        raise RuntimeError(f"Unrecognized Goal Plus installation: {path}")
    return value


def source_owner(package: Path) -> Path:
    receipt = read_receipt(package)
    return Path(receipt["source"]).resolve() if receipt else package.resolve()


@contextlib.contextmanager
def locked():
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "installation.json"
    with (root / "install.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if marker.exists():
            if json.loads(marker.read_text()).get("schema") != SCHEMA:
                raise RuntimeError(f"Unrecognized installation directory: {root}")
        else:
            # Bootstrap and the separately owned Agent POSIX SDK share this root.
            if any(p.name not in {"bootstrap", "install.lock", "thinkthread-agent-posix"} for p in root.iterdir()):
                raise RuntimeError(f"Refusing unmanaged installation directory: {root}")
            marker.write_text(json.dumps({"schema": SCHEMA}))
        yield


def prepare(source: Path, *, dry_run: bool = False) -> Path:
    source = source.resolve()
    captured = list(files(source))
    build = fingerprint(source, captured)
    release = home() / "releases" / build
    package = release / "package"
    python = release / "venv" / "bin" / "python"
    if dry_run:
        print(f"Would prepare managed Python {PYTHON_VERSION}: {release}")
        return package
    with locked():
        receipt = read_receipt(package)
        if receipt:
            verify(package)
            return package
        if release.exists():
            raise RuntimeError(f"Incomplete managed release; remove it after inspection: {release}")
        release.mkdir(parents=True)
        try:
            for relative, content in captured:
                target = package / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod((source / relative).stat().st_mode & 0o777)
            manifest_path = package / ".codex-plugin/plugin.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text())
                manifest["version"] = manifest["version"].split("+")[0] + "+" + build
                manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
                # Only prepared releases may participate in Codex discovery.
                marketplace = json.loads((package / "scripts/templates/codex-marketplace.json").read_text())
                marketplace_path = package / ".agents/plugins/marketplace.json"
                marketplace_path.parent.mkdir(parents=True, exist_ok=True)
                marketplace_path.write_text(json.dumps(marketplace, indent=2) + "\n")
            # Codex removes old cache directories on refresh. Commands already
            # loaded by an older session must keep working from its release.
            launcher = package / "scripts/run-runtime.sh"
            for name in ("hooks/hooks.json", ".codex/hooks.example.json"):
                path = package / name
                if path.is_file():
                    hooks = json.loads(path.read_text())
                    for groups in hooks["hooks"].values():
                        for group in groups:
                            for handler in group["hooks"]:
                                handler["command"] = "bash " + shlex.quote(str(launcher)) + " --goal-plus-host-hook"
                    path.write_text(json.dumps(hooks, indent=2) + "\n")
            mcp_path = package / ".mcp.json"
            if mcp_path.is_file():
                mcp = json.loads(mcp_path.read_text())
                mcp["mcpServers"]["goal-plus"]["args"][0] = str(launcher)
                mcp_path.write_text(json.dumps(mcp, indent=2) + "\n")
            info = {"schema": SCHEMA, "build": build, "source": str(source),
                    "package": str(package), "python": str(python), "python_version": PYTHON_VERSION}
            (package / "src/goal_plus/_installation.json").write_text(json.dumps(info))
            env = dict(os.environ)
            for key in tuple(env):
                if key.startswith("UV_") or key in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX"}:
                    env.pop(key)
            env["UV_PYTHON_INSTALL_DIR"] = str(home() / "bootstrap/python")
            env["UV_PROJECT_ENVIRONMENT"] = str(release / "venv")
            uv = os.environ.get("GOAL_PLUS_UV") or shutil.which("uv")
            if not uv:
                raise RuntimeError("uv is required; use ./install.sh to bootstrap it")
            subprocess.run([uv, "sync", "--no-config", "--managed-python", "--python", PYTHON_VERSION,
                            "--locked", "--no-dev", "--no-editable", "--project", str(package)],
                           env=env, check=True)
            # Verify the installed module and child interpreter before publishing.
            result = subprocess.run([str(python), "-I", "-m", "goal_plus.installation"],
                                    capture_output=True, text=True, check=True)
            actual = json.loads(result.stdout)
            if actual["build"] != build or actual["python"] != str(python):
                raise RuntimeError("Managed runtime did not load the expected build/interpreter")
            (package / ".goal-plus-runtime.json").write_text(json.dumps(info))
            (package / ".goal-plus-python").write_text(str(python) + "\n")
            (package / ".goal-plus-build").write_text(build + "\n")
            return package
        except BaseException:
            shutil.rmtree(release)
            raise


def verify(package: Path) -> dict:
    receipt = read_receipt(package)
    if receipt is None:
        raise RuntimeError(f"Goal Plus runtime is not prepared: {package}")
    result = subprocess.run([receipt["python"], "-I", "-m", "goal_plus.installation"],
                            capture_output=True, text=True, check=True)
    actual = json.loads(result.stdout)
    if actual.get("build") != receipt["build"] or actual.get("python") != receipt["python"]:
        raise RuntimeError(f"Goal Plus runtime identity mismatch: {package}")
    return actual


def binding_path(host: str, config: Path) -> Path:
    key = hashlib.sha256(str(config.expanduser().resolve()).encode()).hexdigest()[:16]
    return home() / "bindings" / f"{host}-{key}.json"


def bind(host: str, config: Path, package: Path, *, dry_run: bool = False) -> None:
    if dry_run:
        return
    with locked():
        path = binding_path(host, config)
        path.parent.mkdir(exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"schema": SCHEMA, "host": host, "config": str(config), "package": str(package)}))
        temp.replace(path)


def unbind(host: str, config: Path, *, dry_run: bool = False) -> None:
    if not dry_run and home().exists():
        with locked():
            binding_path(host, config).unlink(missing_ok=True)


def cleanup(*, dry_run: bool = False) -> None:
    if not (home() / "releases").exists():
        return
    if dry_run:
        print(f"Would remove unreferenced managed releases in {home() / 'releases'}")
        return
    with locked():
        pinned = {Path(json.loads(p.read_text())["package"]).resolve().parent
                  for p in (home() / "bindings").glob("*.json")}
        for release in (home() / "releases").iterdir():
            if release.is_symlink():
                raise RuntimeError(f"Refusing symlink release: {release}")
            if release.resolve() in pinned:
                continue
            receipt = read_receipt(release / "package")
            if receipt is None or receipt["build"] != release.name or Path(receipt["package"]).resolve() != release / "package":
                raise RuntimeError(f"Refusing unrecognized release: {release}")
            shutil.rmtree(release)
