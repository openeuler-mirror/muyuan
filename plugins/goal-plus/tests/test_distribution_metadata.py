from __future__ import annotations

from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from goal_plus import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_dependency_lock_matches_declared_constraints() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    with (ROOT / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    package = next(item for item in lock["package"] if item["name"] == project["name"])

    declarations = list(project["dependencies"])
    for values in project.get("optional-dependencies", {}).values():
        declarations.extend(values)

    # uv rewrites environment markers; this offline gate checks declared constraints.
    # Full resolution and marker consistency remain covered by uv lock --check.
    def constraints(requirement: Requirement) -> tuple:
        return (canonicalize_name(requirement.name), requirement.specifier,
                frozenset(requirement.extras), requirement.url)

    expected = {constraints(Requirement(value)) for value in declarations}
    actual = set()
    for entry in package["metadata"]["requires-dist"]:
        requirement = Requirement(entry["name"] + entry.get("specifier", ""))
        requirement.extras = set(entry.get("extras", []))
        requirement.url = entry.get("url")
        actual.add(constraints(requirement))

    assert actual == expected, "Run uv lock --no-config --managed-python --python 3.12"
    assert lock["requires-python"] == project["requires-python"]
    assert set(package["metadata"].get("provides-extras", [])) == set(
        project.get("optional-dependencies", {})
    )


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
        ROOT / "assets/codex/plugin.json",
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
