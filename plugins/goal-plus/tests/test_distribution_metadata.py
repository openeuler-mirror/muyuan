from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from goal_plus import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_hatch_build_uses_runtime_version_source() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    assert config["project"]["dynamic"] == ["version"]
    assert "version" not in config["project"]
    assert config["tool"]["hatch"]["version"]["path"] == (
        "src/goal_plus/_version.py"
    )
    assert __version__ == "0.1.0"


def test_operational_docs_do_not_reference_retired_checkouts() -> None:
    paths = (
        ROOT / "README.md",
        ROOT / "README_zh.md",
        ROOT / "docs" / "pi.md",
        ROOT / "docs" / "codex.md",
        ROOT / ".codex-plugin" / "plugin.json",
    )
    retired = (
        "/Users/qiaolina/Code/goal-plus",
        "oh-my-knowledge/code/goal-plus",
        ".[dev,report]",
    )

    for path in paths:
        text = path.read_text(encoding="utf-8")
        for marker in retired:
            assert marker not in text, f"{path.relative_to(ROOT)} contains {marker}"


def test_codex_docs_use_the_shipped_mcp_server_name() -> None:
    paths = (ROOT / "docs" / "codex.md",)

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "[mcp_servers.goal-plus]" in text
        assert "[mcp_servers.gp-runtime]" not in text
