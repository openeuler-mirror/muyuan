from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "model-optimize"


def test_torch_cpu_prompts_set_ten_minute_whole_goal_limit() -> None:
    for name in ("pi-goal-prompt.md", "codex-goal-prompt.md"):
        prompt = (EXAMPLE / name).read_text(encoding="utf-8")

        assert "entire Goal Plus task" in prompt
        assert "within 10 minutes of\ncreation" in prompt
        assert "Reserve sufficient time within that limit" in prompt
        assert "complete final validation, promotion" in prompt
