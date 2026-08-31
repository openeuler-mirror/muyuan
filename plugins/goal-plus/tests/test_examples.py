from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from goal_plus.goal_plus import parse_goal_plus_command_config
from goal_plus.models import SearchSpec


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_ROOT = ROOT / "examples"
pytestmark = pytest.mark.example


EXAMPLES = {
    "ascendc-direct-search": {
        "prompts": ("pi-goal-prompt.md",),
        "required": (
            "README.md",
            "SPEC_DISCOVERY.md",
            "knowledge.sources.json",
            "materialize_knowledge.py",
            "request.schema.json",
            "template/SOURCE.json",
        ),
    },
    "circle-packing": {
        "prompts": (
            "pi-goal-prompt.md",
            "codex-goal-prompt.md",
            "pi-goal-prompt-untimed.md",
            "codex-goal-prompt-untimed.md",
        ),
        "required": (
            "README.md",
            "search-spec.json",
            "workspace-template/initial_program.py",
            "workspace-template/.goal-plus-verifiers/circle_packing_score.py",
        ),
    },
    "circle-packing-git-apply": {
        "prompts": (
            "pi-goal-prompt.md",
            "codex-goal-prompt.md",
            "pi-goal-prompt-untimed.md",
            "codex-goal-prompt-untimed.md",
        ),
        "required": (
            "README.md",
            "prepare_workspace.py",
            "search-spec.json",
        ),
    },
    "edgebench-ad-placement": {
        "prompts": ("pi-goal-prompt.md",),
        "required": (
            "README.md",
            "search-spec.json",
            "workspace-template/solution.cpp",
            "workspace-template/.goal-plus-verifiers/ad_local_score.py",
        ),
    },
    "model-optimize": {
        "prompts": ("pi-goal-prompt.md", "codex-goal-prompt.md"),
        "required": (
            "README.md",
            "workspace-template/model.py",
            "workspace-template/.goal-plus-verifiers/torch_cpu_tokens.py",
        ),
    },
    "vliw-kernel-optimization": {
        "prompts": ("pi-goal-prompt.md",),
        "required": (
            "README.md",
            "workspace-template/solution.py",
            "workspace-template/.goal-plus-verifiers/vliw_score.py",
        ),
    },
}


def _prompt_paths() -> list[Path]:
    return [
        EXAMPLES_ROOT / name / prompt
        for name, contract in EXAMPLES.items()
        for prompt in contract["prompts"]
    ]


def test_supported_examples_have_their_entry_assets() -> None:
    actual = {
        path.name
        for path in EXAMPLES_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    }
    assert actual == set(EXAMPLES)

    for name, contract in EXAMPLES.items():
        example = EXAMPLES_ROOT / name
        for relative in (*contract["required"], *contract["prompts"]):
            assert (example / relative).exists(), f"{name}: missing {relative}"


def test_prompt_entries_use_explicit_config_and_only_circle_routes_models() -> None:
    routed_prompts = {
        EXAMPLES_ROOT / "circle-packing" / "codex-goal-prompt.md",
        EXAMPLES_ROOT / "circle-packing" / "codex-goal-prompt-untimed.md",
    }

    for path in _prompt_paths():
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        expected_entry = "$goal-plus" if path.name.startswith("codex-") else "/goal-plus"
        assert first_line.startswith(f"{expected_entry} ")
        assert "max_parallel=2" in first_line
        assert "workspace_backend=git_worktree" in first_line
        assert "strategy=agent_guided" in first_line
        assert " -- " not in first_line
        expected_promotion = (
            "artifact_only" if "read-only" in first_line else "apply"
        )
        assert f"promotion_mode={expected_promotion}" in first_line
        objective, config = parse_goal_plus_command_config(
            first_line.split(" ", 1)[1]
        )
        assert objective
        assert config.max_parallel == 2
        assert config.workspace_backend == "git_worktree"
        assert config.promotion_mode == expected_promotion
        assert config.strategy == "agent_guided"
        if path in routed_prompts:
            assert "annotator=gpt-5.6-terra" in first_line
            assert "workers=gpt-5.6-terra,gpt-5.6-luna" in first_line
            assert "main=" not in first_line
        else:
            assert re.search(r"\b(main|annotator|workers)=", first_line) is None


def test_circle_untimed_prompts_define_no_time_condition() -> None:
    forbidden = re.compile(
        r"\b(?:time|timed|runtime|deadline|minute|minutes|second|seconds|"
        r"hour|hours|lease|closeout)\b|(?:时间|分钟|小时|秒)",
        re.IGNORECASE,
    )
    for example_name in ("circle-packing", "circle-packing-git-apply"):
        for name in (
            "pi-goal-prompt-untimed.md",
            "codex-goal-prompt-untimed.md",
        ):
            text = (EXAMPLES_ROOT / example_name / name).read_text(
                encoding="utf-8"
            )
            assert forbidden.search(text) is None


def test_circle_timed_prompts_treat_the_preset_as_observational() -> None:
    for example_name in ("circle-packing", "circle-packing-git-apply"):
        for name in ("pi-goal-prompt.md", "codex-goal-prompt.md"):
            text = (EXAMPLES_ROOT / example_name / name).read_text(
                encoding="utf-8"
            )
            normalized_text = " ".join(text.split())
            assert "preset task duration is 5 minutes" in normalized_text
            assert "mark the Goal Plus record complete" in normalized_text
            assert "do not label the difference late" in normalized_text


@pytest.mark.parametrize(
    "relative_path",
    (
        "circle-packing/search-spec.json",
        "edgebench-ad-placement/search-spec.json",
    ),
)
def test_checked_in_example_search_specs_are_loadable(relative_path: str) -> None:
    payload = json.loads((EXAMPLES_ROOT / relative_path).read_text(encoding="utf-8"))
    spec = SearchSpec.model_validate(payload)

    assert not Path(spec.source_path).is_absolute()
    assert (ROOT / spec.source_path).is_dir()
    assert spec.process_verifiers
    assert spec.workspace.promotion_mode == "artifact_only"
