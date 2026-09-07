from __future__ import annotations

from pathlib import Path

import pytest

from tests.st.hosts import (
    HOST_BY_MARKER,
    ST_ACTIVE_ENV,
    link_host_assets,
    st_host_from_marker_names,
)
from tests.st.helpers.codex_runner import CodexRunner


def test_st_host_markers_select_one_agent() -> None:
    assert HOST_BY_MARKER["st_codex"].kind == "codex"
    assert HOST_BY_MARKER["st_pi_rpc"].kind == "pi-rpc"

    assert st_host_from_marker_names(["st", "st_codex"]) == "codex"
    assert st_host_from_marker_names(["st", "st_pi_rpc"]) == "pi-rpc"
    assert st_host_from_marker_names(["st"]) == "codex"

    with pytest.raises(ValueError, match="multiple ST host markers"):
        st_host_from_marker_names(["st", "st_codex", "st_pi_rpc"])


@pytest.mark.codex
def test_codex_runner_uses_exec_with_terra_model(tmp_path: Path) -> None:
    runner = CodexRunner(project_root=tmp_path / "project", log_dir=tmp_path)

    cmd = runner._build_cmd("run the prompt")

    assert cmd[:2] == ["codex", "exec"]
    assert ["-m", "gpt-5.6-terra"] == [
        cmd[cmd.index("-m")],
        cmd[cmd.index("-m") + 1],
    ]
    assert "-c" not in cmd
    assert ["-C", str(tmp_path / "project")] == [
        cmd[cmd.index("-C")],
        cmd[cmd.index("-C") + 1],
    ]
    assert "--skip-git-repo-check" in cmd
    assert "--dangerously-bypass-hook-trust" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "do not run pytest" in cmd[-1]
    assert "Codex $goal-plus system test" in cmd[-1]
    assert "Decide autonomously whether Search adds value" in cmd[-1]
    assert "Do not ask for or wait for user confirmation" in cmd[-1]
    assert "run the prompt" in cmd[-1]


@pytest.mark.codex
def test_codex_runner_preserves_plain_prompt_without_goal_plus_prefix(
    tmp_path: Path,
) -> None:
    runner = CodexRunner(project_root=tmp_path / "project", log_dir=tmp_path)
    prompt = "帮我用 goal-plus 完成入口看护测试"

    cmd = runner._build_plain_cmd(prompt)

    assert cmd[:2] == ["codex", "exec"]
    assert cmd[-1] == prompt
    assert not cmd[-1].startswith("$goal-plus")


def test_st_active_env_guard_name_is_stable() -> None:
    assert ST_ACTIVE_ENV == "GOAL_PLUS_ST_ACTIVE"


def test_link_host_assets_supports_maintained_hosts(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    for name in (".codex", ".pi"):
        path = source / name
        path.mkdir()
    project = tmp_path / "run"
    project.mkdir()

    link_host_assets(project, source)

    for name in (".codex", ".pi"):
        assert (project / name).exists()


@pytest.mark.codex
def test_codex_redispatch_prompt_names_required_runtime_evidence() -> None:
    prompt = Path("tests/st/prompts/codex_redispatch.md").read_text(encoding="utf-8")

    assert "gpt-5.6-terra" in prompt
    assert "worker_host" not in prompt
    assert "search_redispatch_candidate" in prompt
    assert "two different agent_session_id" in prompt
    assert "parent_watchdog" in prompt


@pytest.mark.codex
def test_codex_circle_packing_cycle_has_isolated_source_and_valid_layouts(tmp_path: Path) -> None:
    import json
    import subprocess
    import sys

    from goal_plus.models import SearchSpec
    from tests.st.helpers.circle_cycle import prepare_circle
    from tests.st.test_st_scenarios import SCENARIO_CASES

    case = next(
        case for case in SCENARIO_CASES if case.id == "codex_circle_packing_cycle"
    )
    assert {mark.name for mark in case.marks} == {"st", "st_codex"}

    hashes = prepare_circle(tmp_path, "gpt-5.6-sol", "low")
    assert set(hashes) == {"initial_program.py", "evaluator.py"}
    source = tmp_path / "source"
    spec = SearchSpec.model_validate_json((source / "spec.json").read_text())
    assert "worker_host" not in spec.strategy.model_dump()
    assert spec.strategy.worker_launch.model == "gpt-5.6-sol"
    assert spec.strategy.evidence_annotator.reasoning_effort == "low"
    assert (source / ".git").is_dir() and (tmp_path / ".git").is_dir()
    for layout, expected in (("rectangular", 2.166664066666667), ("staggered", 2.4389636673796344)):
        result = subprocess.run(
            [sys.executable, "evaluator.py", f"candidate-hints/{layout}.py"],
            cwd=source, capture_output=True, text=True, check=True,
        )
        output = json.loads(result.stdout)
        assert output["validity"] == 1
        assert output["sum_radii"] == pytest.approx(expected)


@pytest.mark.codex
def test_st_scenario_cases_cover_codex() -> None:
    from tests.st.test_st_scenarios import SCENARIO_CASES

    marker_names = {
        mark.name
        for case in SCENARIO_CASES
        for mark in case.marks
    }

    assert marker_names == {"st", "st_codex"}


@pytest.mark.pi
def test_pi_rpc_parallel_loop_cycle_is_selectable_by_marker() -> None:
    from tests.st import test_st_pi_rpc

    test_func = test_st_pi_rpc.test_pi_rpc_parallel_loop_cycle
    marker_names = {mark.name for mark in getattr(test_func, "pytestmark", [])}

    assert {"st", "st_pi_rpc"} <= marker_names
