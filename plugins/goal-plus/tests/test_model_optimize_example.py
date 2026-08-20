from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "model-optimize"


def test_torch_cpu_prompts_set_ten_minute_whole_goal_limit() -> None:
    for name in ("pi-goal-prompt.md", "codex-goal-prompt.md"):
        prompt = (EXAMPLE / name).read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())

        assert "entire Goal Plus task" in prompt
        assert "within 10 minutes of\ncreation" in prompt
        assert "after 8 minutes" in prompt
        assert "No worker lease may exceed 6" in prompt
        assert "complete final validation, promotion" in normalized
