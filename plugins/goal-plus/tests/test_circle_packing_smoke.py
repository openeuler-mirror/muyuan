from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools
from tests._runtime_helpers import start_host_verified_goal


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
    monkeypatch.chdir(EXAMPLE)
    root = tmp_path / ".gp"
    start_host_verified_goal(root, EXAMPLE)
    tools = SearchTools(FileSearchRuntime(root))

    frozen = tools.search_freeze_spec(spec, [str(verifier.relative_to(EXAMPLE))])
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
    with pytest.raises(RuntimeError, match="artifact-only"):
        tools.search_apply_promotion(run_id)
    report = tools.search_report(run_id)

    assert initial_program.read_bytes() == baseline
    assert Path(promoted["artifact_path"]).is_file()
    assert Path(report["report_path"]).is_file()
    assert Path(report["html_report_path"]).is_file()
