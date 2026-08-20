"""Shared filesystem defaults for Goal Plus/Search runtime state."""

from __future__ import annotations

from pathlib import Path


DEFAULT_RUNTIME_ROOT = ".gp"
LEGACY_RUNTIME_ROOT = ".search"


def host_assets_root() -> Path:
    """Return checkout assets when editable, otherwise wheel-packaged assets."""

    checkout_root = Path(__file__).resolve().parents[2]
    if (checkout_root / ".codex-plugin" / "plugin.json").is_file():
        return checkout_root
    return Path(__file__).resolve().parent / "_plugin_root"
