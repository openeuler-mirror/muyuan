from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "circle-packing"
WORKSPACE_TEMPLATE = EXAMPLE / "workspace-template"
pytestmark = pytest.mark.example


def test_circle_packing_public_baseline_emits_goal_plus_metric() -> None:
    completed = subprocess.run(
        [sys.executable, ".goal-plus-verifiers/circle_packing_score.py"],
        cwd=WORKSPACE_TEMPLATE,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["valid"] is True
    assert report["sum_radii"] == pytest.approx(1.7983485547704765)


def test_circle_packing_prompt_is_a_five_minute_interactive_pi_goal() -> None:
    prompt = (EXAMPLE / "pi-goal-prompt.md").read_text(encoding="utf-8")
    readme = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    first_line = prompt.splitlines()[0]
    normalized = " ".join(prompt.split())

    assert first_line.startswith("/goal-plus max_parallel=2")
    assert " main=" not in first_line
    assert " annotator=" not in first_line
    assert " workers=" not in first_line
    assert "max_parallel=2" in first_line
    assert "current directory" in first_line
    assert "workspace-template/.goal-plus-verifiers/circle_packing_score.py" in prompt
    assert "within 5 minutes" in prompt
    assert "after 4 minutes" in prompt
    assert "No worker lease may\nexceed 3 minutes" in prompt
    assert "Git worktree-backed Search" in prompt
    assert "maintained `agent_guided` strategy" in prompt
    assert "`search-spec.json` as the authoritative" in prompt
    assert '`source_path` to `"workspace-template"`' in normalized
    assert "do not apply it back to `workspace-template/`" in prompt
    assert "do not run `git apply`" in prompt
    assert "report and promotion artifact are the only deliverables" in normalized
    assert "remains unchanged" in prompt
    assert "regular row/grid construction" in normalized
    assert "cd examples/circle-packing" in readme
    assert "prepare_workspace.py" not in readme
    assert "not apply the selected patch back" in readme
    assert "\npi\n" in readme
    assert "does not pin `main`, `annotator`, or `workers`" in readme
    assert "Then paste the complete contents of" in readme
    assert "$(cat examples/circle-packing/pi-goal-prompt.md)" not in readme


def test_circle_packing_prompt_is_a_five_minute_interactive_codex_goal() -> None:
    prompt = (EXAMPLE / "codex-goal-prompt.md").read_text(encoding="utf-8")
    readme = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    first_line = prompt.splitlines()[0]
    normalized = " ".join(prompt.split())

    assert first_line.startswith("$goal-plus annotator=")
    assert " main=" not in first_line
    assert "annotator=gpt-5.6-terra" in first_line
    assert "workers=gpt-5.6-terra,gpt-5.6-luna" in first_line
    assert "max_parallel=2" in first_line
    assert "current directory" in first_line
    assert "workspace-template/.goal-plus-verifiers/circle_packing_score.py" in prompt
    assert "within 5 minutes" in prompt
    assert "after 4 minutes" in prompt
    assert "No worker lease may\nexceed 3 minutes" in prompt
    assert "Git worktree-backed Search" in prompt
    assert "maintained `agent_guided` strategy" in prompt
    assert "`search-spec.json` as the authoritative" in prompt
    assert '`source_path` to `"workspace-template"`' in normalized
    assert "do not apply it back to `workspace-template/`" in prompt
    assert "do not run `git apply`" in prompt
    assert "report and promotion artifact are the only deliverables" in normalized
    assert "remains unchanged" in prompt
    assert '`worker_host="codex"' in prompt
    assert '`worker_agent_type="search_candidate_agent"' in prompt
    assert "regular row/grid construction" in normalized
    assert "codex -m gpt-5.6-terra -c model_reasoning_effort=max" in readme
    assert "[`codex-goal-prompt.md`](codex-goal-prompt.md)" in readme


def test_circle_packing_example_owns_its_read_only_workspace_template() -> None:
    assert (EXAMPLE / "search-spec.json").is_file()
    assert not (EXAMPLE / "prepare_workspace.py").exists()
    assert (WORKSPACE_TEMPLATE / "initial_program.py").is_file()
    assert (
        WORKSPACE_TEMPLATE
        / ".goal-plus-verifiers"
        / "circle_packing_score.py"
    ).is_file()
    assert not (ROOT / "tests" / "fixtures" / "circle_packing").exists()


def test_circle_packing_promotion_exports_report_without_changing_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_program = WORKSPACE_TEMPLATE / "initial_program.py"
    baseline = initial_program.read_bytes()
    spec = json.loads((EXAMPLE / "search-spec.json").read_text(encoding="utf-8"))
    verifier = (
        WORKSPACE_TEMPLATE
        / ".goal-plus-verifiers"
        / "circle_packing_score.py"
    )
    monkeypatch.chdir(ROOT)
    tools = SearchTools(FileSearchRuntime(tmp_path / ".gp"))

    frozen = tools.search_freeze_spec(spec, [str(verifier.relative_to(ROOT))])
    run_id = tools.search_create(frozen["frozen_spec_id"])["run_id"]
    plan = tools.search_plan_next(run_id, 2)
    tasks = tools.search_start_batch(
        run_id,
        plan["plan_id"],
        proposals=[
            {"intent": "regular baseline"},
            {"intent": "staggered baseline"},
        ],
    )
    for task in tasks:
        session = tools.search_start_agent_session(
            run_id,
            task["candidate_id"],
            {"goal": "verify the unchanged candidate baseline"},
        )
        tools.search_get_agent_context(session["agent_session_id"])
        tools.search_run_verifier(
            run_id,
            task["candidate_id"],
            agent_session_id=session["agent_session_id"],
            hypothesis="unchanged baseline",
        )

    selected = tools.search_select(run_id)
    promoted = tools.search_promote(run_id, selected["selected_candidate_id"])
    report = tools.search_report(run_id)

    assert initial_program.read_bytes() == baseline
    assert Path(promoted["artifact_path"]).is_file()
    assert Path(report["report_path"]).is_file()
    assert Path(report["html_report_path"]).is_file()
