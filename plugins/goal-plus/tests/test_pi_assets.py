from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.pi


def test_pi_package_exports_the_supported_user_assets() -> None:
    expected = {
        ".pi/prompts/goal-plus.md",
        ".pi/skills/goal-plus/SKILL.md",
        ".pi/extensions/goal-plus.ts",
    }
    assert all((ROOT / path).is_file() for path in expected)

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert package["pi"]["extensions"] == [".pi/extensions/goal-plus.ts"]
    assert package["pi"]["skills"] == [".pi/skills/goal-plus/SKILL.md"]
    skill_files = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / ".pi" / "skills").glob("*/SKILL.md")
    )
    assert skill_files == [".pi/skills/goal-plus/SKILL.md"]


def test_pi_search_recovery_tools_are_registered_only_for_main() -> None:
    text = (ROOT / ".pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    main = text.split("const mainTools = [", 1)[1].split("];", 1)[0]
    worker = text.split("const workerTools = [", 1)[1].split("];", 1)[0]
    for name in ("search_recovery_status", "search_prepare_recovery", "search_recover_candidate"):
        assert f'"{name}"' in main
        assert f'"{name}"' not in worker
        assert f"{name}: Type.Object" in text


def test_pi_package_manifest_exports_only_the_user_integration() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    assert package["name"] == "goal-plus"
    assert "pi-package" in package["keywords"]
    assert package["pi"] == {
        "extensions": [".pi/extensions/goal-plus.ts"],
        "skills": [".pi/skills/goal-plus/SKILL.md"],
    }


def test_pyproject_exposes_pi_console_scripts() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["scripts"]

    assert scripts["goal-plus-pi-worker"] == "goal_plus.pi_worker:main"
    assert scripts["goal-plus-pi-tool"] == "goal_plus.pi_tool:main"


def test_pi_compatibility_prompt_delegates_to_the_skill() -> None:
    prompt = (ROOT / ".pi" / "prompts" / "goal-plus.md").read_text(
        encoding="utf-8"
    )

    assert "/skill:goal-plus" in prompt
    assert "$ARGUMENTS" in prompt
