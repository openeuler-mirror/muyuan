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
    workspace_backend: str = "git_worktree",
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
    data["workspace"] = {"backend": workspace_backend}
    data["strategy"]["config"] = {
        "global_evidence_mode": global_evidence_mode,
    }
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"],
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


def test_reference_workspace_is_exact_browsable_and_disposable(
    tmp_path: Path,
) -> None:
    runtime, run_id, candidates = _search_with_two_candidates(tmp_path)
    source_id, source_session, source_workspace = candidates[0]
    _, requester_session, _ = candidates[1]
    tools = SearchTools(runtime)
    context = tools.search_get_agent_context(requester_session)
    assert context["workspace_access"]["confinement"] == "soft_path"
    assert context["workspace_access"]["bootstrap_required"] is True

    (source_workspace / "initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    tools.search_run_verifier(
        run_id,
        source_id,
        agent_session_id=source_session,
        hypothesis="Set the first candidate value",
    )
    [evidence] = [
        item
        for item in tools.search_get_global_evidence(requester_session)
        if item["candidate_id"] == source_id
    ]
    assert evidence["reference_available"] is True
    assert evidence["artifact_ref"]["kind"] == "git_commit"
    assert evidence["artifact_ref"]["provider"] == "git_worktree"

    opened = tools.search_reference_open(
        requester_session,
        source_id,
        evidence["iteration"],
    )
    reference_id = opened["reference_id"]
    root = Path(opened["access"]["root"])
    assert opened["access"]["kind"] == "filesystem_path"
    assert opened["access"]["readonly_contract"] == "soft_detected"
    assert root.name.startswith(f"ref-{source_id}-verifier-input-001-")
    assert root.is_dir()
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip() == evidence["artifact_ref"]["id"]
    assert subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=root, text=True
    ).strip() == ""

    listing = tools.search_reference_list(
        requester_session,
        reference_id,
    )
    assert "initial_program.py" in {item["path"] for item in listing["entries"]}
    assert ".git" not in {item["path"] for item in listing["entries"]}
    read = tools.search_reference_read(
        requester_session,
        reference_id,
        "initial_program.py",
    )
    assert read["content"] == "VALUE = 1\n"
    inspected = tools.search_reference_exec(
        requester_session,
        reference_id,
        ["git", "show", "HEAD:initial_program.py"],
    )
    assert inspected["returncode"] == 0
    assert inspected["stdout"] == "VALUE = 1\n"

    (source_workspace / "initial_program.py").write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )
    tools.search_run_verifier(
        run_id,
        source_id,
        agent_session_id=source_session,
        hypothesis="Set a later candidate value",
    )
    assert tools.search_reference_read(
        requester_session,
        reference_id,
        "initial_program.py",
    )["content"] == "VALUE = 1\n"

    with pytest.raises(PermissionError, match="escapes"):
        tools.search_reference_read(
            requester_session,
            reference_id,
            "../../outside.txt",
        )
    with pytest.raises(PermissionError, match="not read-only"):
        tools.search_reference_exec(
            requester_session,
            reference_id,
            ["git", "checkout", "HEAD~1"],
        )
    with pytest.raises(PermissionError, match="execute or write"):
        tools.search_reference_exec(
            requester_session,
            reference_id,
            ["git", "log", "--all"],
        )
    with pytest.raises(PermissionError, match="Git metadata"):
        tools.search_reference_read(
            requester_session,
            reference_id,
            ".git",
        )

    closed = tools.search_reference_close(requester_session, reference_id)
    assert closed["state"] == "closed"
    assert closed["integrity_status"] == "clean"
    assert not root.exists()


def test_reference_workspace_detects_direct_mutation_before_cleanup(
    tmp_path: Path,
) -> None:
    runtime, run_id, candidates = _search_with_two_candidates(tmp_path)
    source_id, source_session, source_workspace = candidates[0]
    _, requester_session, _ = candidates[1]
    (source_workspace / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")
    runtime.run_verifier(
        run_id,
        source_id,
        agent_session_id=source_session,
        hypothesis="Create reference source",
    )
    opened = runtime.open_reference_workspace(requester_session, source_id, 1)
    assert opened.access.root is not None
    (opened.access.root / "initial_program.py").write_text(
        "VALUE = 999\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="changed after materialization"):
        runtime.read_reference_workspace(
            requester_session,
            opened.reference_id,
            "initial_program.py",
        )
    closed = runtime.close_reference_workspace(
        requester_session,
        opened.reference_id,
    )
    assert closed.integrity_status == "contaminated"
    assert not opened.access.root.exists()


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
        workspace_backend="copy",
    )
    source_id, source_session, source_workspace = candidates[0]
    _, requester_session, _ = candidates[1]
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
    assert runtime.read_reference_workspace(
        requester_session,
        opened.reference_id,
        "initial_program.py",
    )["content"] == "VALUE = 4\n"
    runtime.close_reference_workspace(requester_session, opened.reference_id)
