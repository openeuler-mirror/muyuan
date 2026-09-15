from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from goal_plus.models import SearchSpec
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools
from tests._runtime_helpers import make_project, spec_for


def _search_with_two_candidates(
    tmp_path: Path,
    *,
    global_evidence_mode: str = "auto",
    workspace_provider: str = "git_worktree",
) -> tuple[FileSearchRuntime, str, list[tuple[str, str, Path]]]:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "value = Path('initial_program.py').read_text().split('=', 1)[1].strip()\n"
        "print(json.dumps({'combined_score': float(value)}))\n",
        encoding="utf-8",
    )
    data = spec_for(project, max_parallel=2).model_dump(mode="json")
    data["workspace"] = {"provider": workspace_provider}
    data["strategy"]["config"] = {
        "global_evidence_mode": global_evidence_mode,
    }
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"], agent_harness="codex",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    sessions = [
        runtime.start_agent_session(run_id, task.candidate_id)
        for task in tasks
    ]
    return runtime, run_id, [
        (task.candidate_id, session.agent_session_id, task.workspace)
        for task, session in zip(tasks, sessions, strict=True)
    ]


def test_reference_workspace_is_exact_browsable_and_reused(
    tmp_path: Path,
) -> None:
    runtime, run_id, candidates = _search_with_two_candidates(tmp_path)
    from types import SimpleNamespace

    state = runtime.references.state
    runtime.references.state = SimpleNamespace(
        read=state.read, write=state.write, destination=state.destination,
        lock_run=state.lock_run, read_run=state.read_run,
        read_frozen_spec=state.read_frozen_spec, read_candidate=state.read_candidate,
    )
    source_id, source_session, source_workspace = candidates[0]
    _, requester_session, _ = candidates[1]
    tools = SearchTools(runtime)

    (source_workspace / "initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    tools.goal_plus_search_run_verifier(
        run_id,
        source_id,
        agent_session_id=source_session,
        hypothesis="Set the first candidate value",
    )
    [evidence] = [
        item
        for item in tools.goal_plus_search_get_global_evidence(requester_session)
        if item["candidate_id"] == source_id
    ]
    assert evidence["reference_available"] is True
    assert evidence["artifact_ref"]["kind"] == "git_commit"
    assert evidence["artifact_ref"]["provider"] == "git_worktree"

    opened = tools.goal_plus_search_reference_open(
        requester_session,
        source_id,
        evidence["iteration"],
    )
    reference_id = opened["reference_id"]
    root = Path(opened["access"]["root"])
    assert opened["access"]["kind"] == "filesystem_path"
    assert opened["access"]["readonly_contract"] == "soft_detected"
    assert root.name == "ref-workspace"
    assert root.is_dir()
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip() == evidence["artifact_ref"]["id"]
    assert subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=root, text=True
    ).strip() == ""
    assert (root / "initial_program.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    # The reference workspace is ordinary checkout state. Local changes are
    # discarded when the worker selects another Evidence commit.
    (root / "initial_program.py").write_text("VALUE = 999\n", encoding="utf-8")

    (source_workspace / "initial_program.py").write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )
    tools.goal_plus_search_run_verifier(
        run_id,
        source_id,
        agent_session_id=source_session,
        hypothesis="Set a later candidate value",
    )
    second_iteration = tools.goal_plus_search_list_iterations(source_session)["items"][-1]["iteration"]
    reopened = tools.goal_plus_search_reference_open(
        requester_session,
        source_id,
        second_iteration,
    )
    assert reopened["reference_id"] == reference_id
    assert Path(reopened["access"]["root"]) == root
    assert (root / "initial_program.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip() == reopened["artifact_ref"]["id"]
    assert len(list(root.parent.iterdir())) == 1
    context = tools.goal_plus_search_get_agent_context(requester_session)
    assert len(context["reference_workspaces"]) == 1
    assert context["reference_workspaces"][0]["artifact_ref"] == reopened["artifact_ref"]


def test_independent_evidence_cannot_open_peer_reference(tmp_path: Path) -> None:
    runtime, run_id, candidates = _search_with_two_candidates(
        tmp_path,
        global_evidence_mode="independent",
    )
    source_id, source_session, source_workspace = candidates[0]
    _, requester_session, _ = candidates[1]
    (source_workspace / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")
    runtime.run_verifier(
        run_id,
        source_id,
        agent_session_id=source_session,
        hypothesis="Create private Evidence",
    )

    with pytest.raises(PermissionError, match="independent Global Evidence"):
        runtime.open_reference_workspace(requester_session, source_id, 1)


def test_copy_backend_uses_the_same_reference_contract(tmp_path: Path) -> None:
    runtime, run_id, candidates = _search_with_two_candidates(
        tmp_path,
        workspace_provider="copy",
    )
    source_id, source_session, source_workspace = candidates[0]
    requester_id, requester_session, requester_workspace = candidates[1]
    (source_workspace / "initial_program.py").write_text("VALUE = 4\n", encoding="utf-8")
    runtime.run_verifier(
        run_id,
        source_id,
        agent_session_id=source_session,
        hypothesis="Create a copy-backed artifact",
    )

    opened = runtime.open_reference_workspace(requester_session, source_id, 1)
    assert opened.artifact_ref.provider == "copy"
    assert opened.access.kind == "filesystem_path"
    assert opened.access.root is not None
    assert (opened.access.root / "initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 4\n"

    (requester_workspace / "initial_program.py").write_text(
        "VALUE = 5\n", encoding="utf-8"
    )
    runtime.run_verifier(
        run_id,
        requester_id,
        agent_session_id=requester_session,
        hypothesis="Create Evidence in the other copy-backed repository",
    )
    switched = runtime.open_reference_workspace(requester_session, requester_id, 1)
    assert switched.reference_id == opened.reference_id
    assert switched.access.root == opened.access.root
    assert switched.access.root is not None
    assert (switched.access.root / "initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 5\n"
