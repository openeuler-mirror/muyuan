"""Install a project-scoped Codex plugin without changing user-global settings."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib

from .online import _atomic_json
from .server import load_settings
from .. import __version__

BEGIN = "# BEGIN BATE META-TOOL (managed)"
END = "# END BATE META-TOOL (managed)"


def _destination(project: Path, relative: str) -> Path:
    path = project / relative
    if not path.resolve().is_relative_to(project):
        raise ValueError(f"Install destination escapes the project: {relative}")
    for candidate in (path, *path.parents):
        if candidate == project:
            break
        if candidate.is_symlink():
            raise ValueError(f"Install destination uses a symlink: {relative}")
    return path


def install(project: Path, *, config: Path | None = None, trace_files=()) -> dict:
    project = project.resolve(strict=True)
    if not project.is_dir():
        raise ValueError("Project must be an existing directory")
    runtime = _destination(project, ".bate-codex")
    plugin = _destination(project, ".bate-codex/plugin")
    marker = _destination(project, ".bate-codex/install.json")
    settings_path = _destination(project, ".bate-codex/config.json")
    market_path = _destination(project, ".agents/plugins/marketplace.json")
    host_path = _destination(project, ".codex/config.toml")
    if (plugin.exists() or settings_path.exists()) and (not marker.exists() or json.loads(marker.read_text()).get("format") != "bate-codex-install-v1"):
        raise ValueError("Plugin directory exists but is not owned by this installer")
    identity = hashlib.sha256(str(project).encode()).hexdigest()[:10]
    marketplace = json.loads(market_path.read_text()) if market_path.exists() else {
        "name": f"bate-local-{identity}", "plugins": []}
    if (not isinstance(marketplace, dict) or not isinstance(marketplace.get("name"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", marketplace["name"])
            or not isinstance(marketplace.get("plugins"), list)):
        raise ValueError("Invalid existing project marketplace")
    entry = {"name": "meta-tool", "source": {"source": "local", "path": "./.bate-codex/plugin"},
             "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
             "category": "Productivity"}
    entries = marketplace["plugins"]
    if not all(isinstance(e, dict) for e in entries):
        raise ValueError("Invalid existing project marketplace entries")
    matches = [e for e in entries if e.get("name") == "meta-tool"]
    if len(matches) > 1 or (matches and matches[0].get("source") != entry["source"]):
        raise ValueError("An unrelated meta-tool plugin already exists in this marketplace")
    marketplace["plugins"] = [e for e in entries if e.get("name") != "meta-tool"] + [entry]

    host_text = host_path.read_text() if host_path.exists() else ""
    tomllib.loads(host_text)
    if BEGIN in host_text and END in host_text and host_text.index(END) < host_text.index(BEGIN):
        raise ValueError("Invalid managed config block order")
    if host_text.count(BEGIN) != host_text.count(END) or host_text.count(BEGIN) > 1:
        raise ValueError("Invalid managed config block; preserve and repair it manually")
    clean = re.sub(re.escape(BEGIN) + r"\n.*?" + re.escape(END) + r"\n?", "", host_text, flags=re.S)
    key = f"meta-tool@{marketplace['name']}"
    existing = tomllib.loads(clean)
    if key in existing.get("plugins", {}):
        raise ValueError("Existing plugin setting is not owned by this installer")
    if "bate" in existing.get("mcp_servers", {}):
        raise ValueError("Project already registers a standalone bate MCP server; remove that entry before installing the plugin")
    market_key = marketplace["name"]
    registered = existing.get("marketplaces", {}).get(market_key)
    source = {"source_type": "local", "source": str(project)}
    if registered is not None and registered != source:
        raise ValueError("A different marketplace already uses this name")
    market_block = ("" if registered is not None else
        f'[marketplaces.{json.dumps(market_key)}]\nsource_type = "local"\nsource = {json.dumps(str(project))}\n')
    host_text = clean.rstrip() + f'\n\n{BEGIN}\n{market_block}[plugins.{json.dumps(key)}]\nenabled = true\n{END}\n'
    tomllib.loads(host_text)

    source_config = config.resolve(strict=True) if config else settings_path
    if source_config.exists():
        settings = load_settings(source_config)
        if source_config == settings_path:
            # Validate in place without materializing implicit runtime defaults
            # on every reinstall. Relative paths still use the same config root.
            settings = json.loads(source_config.read_text())
    else:
        settings = {"mode": "static", "catalog": str(runtime / "catalog.json"),
            "timeout_seconds": 30, "batch_enabled": True, "approved_meta_tools": {},
            "servers": {"project": {"command": sys.executable,
                "args": ["-m", "bate.codex.files", "--root", str(project)], "cwd": str(project)}},
            "bindings": {"read": {"server": "project", "tool": "read", "read_only": True}},
            "online": {"trace_files": [], "state_dir": str(runtime / "online"),
                       "auto_admit_read_only": True}}
    settings.update(session_control=True, start_enabled=False)
    if trace_files:
        online = settings.setdefault("online", {"state_dir": str(runtime / "online"),
                                                 "auto_admit_read_only": True})
        online["trace_files"] = sorted({str((settings_path.parent / p).resolve())
                                        for p in online.get("trace_files", [])} |
                                       {str(Path(p).resolve()) for p in trace_files})
        if len(online["trace_files"]) > 64:
            raise ValueError("At most 64 explicit trace files are supported")

    # All conflict and syntax checks precede writes to existing host settings.
    skill_root = Path(__file__).parent / "skills/meta-tool"
    payloads = {
        ".codex-plugin/plugin.json": {"name": "meta-tool", "version": __version__,
            "description": "BATE session controls, Static/Online workflows and project batch reads",
            "author": {"name": "kkjocker"}, "skills": "./skills/", "mcpServers": "./.mcp.json",
            "interface": {"displayName": "meta-tool", "shortDescription": "BATE controls and project batch reads"}},
        ".mcp.json": {"mcpServers": {"bate": {"command": sys.executable,
            "args": ["-m", "bate.codex", "serve", "--config", str(settings_path)],
            "startup_timeout_sec": 30,
            "tool_timeout_sec": settings.get("online", {}).get("extraction_timeout_seconds", 120)
                                + settings.get("timeout_seconds", 30) + 15}}},
    }
    for relative in [".bate-codex/catalog.json", ".bate-codex/.gitignore",
                     *[".bate-codex/plugin/" + p for p in payloads],
                     ".bate-codex/plugin/skills/meta-tool/SKILL.md",
                     ".bate-codex/plugin/skills/meta-tool/agents/openai.yaml"]:
        _destination(project, relative)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (runtime / "catalog.json").exists():
        _atomic_json(runtime / "catalog.json", {"meta_tools": []})
    _atomic_json(settings_path, settings)
    for relative, payload in payloads.items():
        _atomic_json(plugin / relative, payload)
    for relative in ("SKILL.md", "agents/openai.yaml"):
        target = plugin / "skills/meta-tool" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((skill_root / relative).read_text())
    (runtime / ".gitignore").write_text("*\n")
    _atomic_json(marker, {"format": "bate-codex-install-v1", "python": sys.executable})
    _atomic_json(market_path, marketplace)
    host_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bate-config-", dir=host_path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(host_text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, host_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"installed": True, "project": str(project), "plugin": key,
            "config": str(settings_path), "start_enabled": False,
            "next": "Open a new trusted project session; select meta-tool from / or use $meta-tool on. "
                    "CLI also offers /skills. Check /mcp if tools are unavailable."}
