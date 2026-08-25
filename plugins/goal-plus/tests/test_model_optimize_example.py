from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "model-optimize"
WORKSPACE_TEMPLATE = EXAMPLE / "workspace-template"
VERIFIER = (
    WORKSPACE_TEMPLATE / ".goal-plus-verifiers" / "torch_cpu_tokens.py"
)
pytestmark = pytest.mark.example


def test_torch_cpu_prompts_set_ten_minute_whole_goal_limit() -> None:
    for name in ("pi-goal-prompt.md", "codex-goal-prompt.md"):
        prompt = (EXAMPLE / name).read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())

        assert "entire Goal Plus task" in prompt
        assert "within 10 minutes of\ncreation" in prompt
        assert "after 8 minutes" in prompt
        assert "No worker lease may exceed 6" in prompt
        assert "complete final validation, promotion" in normalized


def test_torch_cpu_pi_prompt_uses_the_installed_model_defaults() -> None:
    prompt = (EXAMPLE / "pi-goal-prompt.md").read_text(encoding="utf-8")
    readme = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    first_line = prompt.splitlines()[0]

    assert first_line.startswith("/goal-plus max_parallel=2")
    assert " main=" not in first_line
    assert " annotator=" not in first_line
    assert " workers=" not in first_line
    assert "\npi\n" in readme
    assert "does not pin `main`, `annotator`, or `workers`" in readme


def test_torch_cpu_prompts_keep_the_tracked_template_read_only() -> None:
    readme = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    assert "cd examples/model-optimize" in readme
    assert "workspace-template/" in readme
    assert "neither\nprompt applies the result back" in readme
    assert "torch-cpu-target/" in readme

    for name in ("pi-goal-prompt.md", "codex-goal-prompt.md"):
        prompt = (EXAMPLE / name).read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "read-only Torch CPU template" in prompt.splitlines()[0]
        assert "workspace-template/.goal-plus-verifiers/torch_cpu_tokens.py" in prompt
        assert "Git worktree-backed Search" in prompt
        assert "do not apply it back to `workspace-template/`" in normalized
        assert "do not run `git apply`" in normalized
        assert "remains unchanged" in prompt

    assert VERIFIER.is_file()
    assert (WORKSPACE_TEMPLATE / "model.py").is_file()
    assert not (EXAMPLE / "torch-cpu-target" / "model.py").exists()


def test_torch_cpu_source_owned_verifier_accepts_the_baseline() -> None:
    for mode in ("--validate", "--rank"):
        completed = subprocess.run(
            [sys.executable, str(VERIFIER), mode],
            cwd=WORKSPACE_TEMPLATE,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        if mode == "--validate":
            assert result == {"valid": True}
        else:
            assert result["tokens_per_second"] > 0
