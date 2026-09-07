from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from goal_plus.agent_hosts import UnsupportedHostCapability
import goal_plus.adaptive_search.scheduler as scheduler_module
from goal_plus.internal_agents import wake_scheduler_after_annotation
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.models import (
    CandidateProposal,
    CandidateRecord,
    DevicePoolSpec,
    RunState,
    ScoreReport,
    SearchSpec,
)
from goal_plus.reporting import build_html_report_data, render_html_report
from goal_plus.infrastructure.process_output import OUTPUT_LIMIT_BYTES as VERIFIER_OUTPUT_LIMIT_BYTES
from goal_plus.infrastructure.verifiers.local import safe_verifier_name
from goal_plus.infrastructure.state.resource_locks import VERIFIER_RESOURCE_LOCK_DIR_ENV
from goal_plus.infrastructure.search_configuration import DEVICE_POOLS_ENV, SearchConfiguration
from goal_plus.infrastructure.state.files import sha256_file
from goal_plus.workspaces import (
    list_files,
    copy_source_tree,
    initialize_workspace_git_baseline,
)
from goal_plus.domain.edit_surface import path_matches
from goal_plus.runtime import FileSearchRuntime
from goal_plus.domain.serialization import canonical_json
from goal_plus.infrastructure.state.files import load_json, write_json
from goal_plus.tools import SearchTools
from tests._runtime_helpers import (
    create_candidate,
    git_commit_all,
    make_project,
    process_is_running,
    spec_for,
    spec_with_host,
    spec_with_strategy,
)


def _guarded_runtime_state(
    runtime: FileSearchRuntime,
    run_id: str,
    candidate_id: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, object]:
    state: dict[str, object] = {
        "run": runtime.search_run.state.read_run(run_id).model_dump(mode="json"),
    }
    if candidate_id is not None:
        state["candidate"] = runtime.candidate_creation.state.read_candidate(
            run_id, candidate_id
        ).model_dump(mode="json")
    if agent_session_id is not None:
        state["agent_session"] = runtime.candidate_sessions.session(
            agent_session_id, run_id=run_id
        ).model_dump(mode="json")
    return state


def test_hash_json_and_path_helpers(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello\n", encoding="utf-8")

    assert sha256_file(file_path) == sha256_file(file_path)
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})
    assert path_matches("src/app.py", ["src/"])
    assert path_matches("initial_program.py", ["*.py"])
    assert not path_matches("evaluator.py", ["initial_program.py"])
    safe_name = safe_verifier_name("../../promotion gate")
    assert "/" not in safe_name
    assert ".." not in safe_name
    assert safe_name.startswith("promotion_gate-")
    assert len(safe_name.rsplit("-", 1)[1]) == 8


def test_copy_source_tree_and_list_files_ignore_runtime_noise(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.py").write_text("x = 1\n", encoding="utf-8")
    (source / ".gp").mkdir()
    (source / ".gp" / "run.json").write_text("{}", encoding="utf-8")
    (source / ".search").mkdir()
    (source / ".search" / "run.json").write_text("{}", encoding="utf-8")
    (source / ".tmp").mkdir()
    (source / ".tmp" / "scratch.py").write_text("print('scratch')\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "keep.pyc").write_text("compiled", encoding="utf-8")

    destination = tmp_path / "dest"
    copy_source_tree(source, destination)

    listed = [path.relative_to(destination).as_posix() for path in list_files(destination)]
    assert listed == ["keep.py"]


def test_source_snapshot_excludes_untracked_gitignored_build_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    (source / ".gitignore").write_text(
        "build/\ndist/\n*.so\n",
        encoding="utf-8",
    )
    (source / "operator.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "vendor").mkdir()
    (source / "vendor" / "tracked.so").write_text("pinned\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore", "operator.py"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "add", "-f", "vendor/tracked.so"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "source",
        ],
        cwd=source,
        check=True,
    )
    (source / "build").mkdir()
    (source / "build" / "object.o").write_text("generated\n", encoding="utf-8")
    (source / "dist").mkdir()
    (source / "dist" / "operator.whl").write_text("generated\n", encoding="utf-8")
    (source / "extension.so").write_text("generated\n", encoding="utf-8")

    destination = tmp_path / "destination"
    copy_source_tree(source, destination)
    baseline = initialize_workspace_git_baseline(destination)

    assert baseline is not None
    assert (destination / "operator.py").is_file()
    assert (destination / "vendor" / "tracked.so").is_file()
    assert not (destination / "build").exists()
    assert not (destination / "dist").exists()
    assert not (destination / "extension.so").exists()
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=destination,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "vendor/tracked.so" in tracked
    assert not any(path.startswith(("build/", "dist/")) for path in tracked)

    runtime = FileSearchRuntime(tmp_path / ".search")
    assert runtime.artifacts.workspace.detect_changed_files(source, destination) == []
    (destination / "build").mkdir()
    (destination / "build" / "object.o").write_text("side effect\n", encoding="utf-8")
    assert runtime.artifacts.workspace.detect_changed_files(source, destination) == ["build/object.o"]


def test_file_search_runtime_defaults_to_gp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    runtime = FileSearchRuntime()

    assert runtime.root_dir == tmp_path / ".gp"
    assert runtime.specs_dir == tmp_path / ".gp" / "specs"
    assert runtime.runs_dir == tmp_path / ".gp" / "runs"
    assert not runtime.root_dir.exists()


def test_write_json_is_readable(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data.json"
    write_json(path, {"ok": True})
    assert path.read_text(encoding="utf-8").strip().startswith("{")


def test_freeze_spec_is_stable_and_copies_verifier(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_for(project)

    first = runtime.freeze_spec(spec, [project / "evaluator.py"])
    second = runtime.freeze_spec(spec, [project / "evaluator.py"])

    assert first.frozen_spec_id == second.frozen_spec_id
    assert first.spec.strategy.inner_agent == "autoresearch"
    assert first.verifier_hashes["evaluator.py"] == sha256_file(project / "evaluator.py")
    assert Path(first.frozen_verifier_paths["evaluator.py"]).exists()


def test_freeze_spec_rejects_unknown_inner_agent_before_workspace_creation(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    payload = spec_for(project).model_dump(mode="json")
    payload["strategy"]["inner_agent"] = "unregistered-agent"

    with pytest.raises(UnsupportedHostCapability, match="unregistered-agent"):
        runtime.freeze_spec(
            SearchSpec.model_validate(payload),
            [project / "evaluator.py"],
        )


def _spec_with_workspace_backend(project: Path, backend: str) -> SearchSpec:
    payload = spec_for(project).model_dump(mode="json")
    payload["workspace"] = {"backend": backend}
    return SearchSpec.model_validate(payload)


def _initialize_source_repository(project: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    git_commit_all(project, "source baseline")


@pytest.mark.parametrize("dirty_kind", ["staged", "unstaged", "untracked"])
def test_freeze_spec_rejects_dirty_git_source_before_preflight(
    tmp_path: Path,
    dirty_kind: str,
) -> None:
    project = make_project(tmp_path)
    _initialize_source_repository(project)
    if dirty_kind == "untracked":
        (project / "scratch.txt").write_text("temporary\n", encoding="utf-8")
    else:
        (project / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")
        if dirty_kind == "staged":
            subprocess.run(
                ["git", "add", "initial_program.py"],
                cwd=project,
                check=True,
            )

    runtime = FileSearchRuntime(tmp_path / ".search")
    with pytest.raises(ValueError, match="DirtySourceWorkspace") as exc_info:
        runtime.freeze_spec(
            _spec_with_workspace_backend(project, "git_worktree"),
            [project / "evaluator.py"],
        )

    expected_path = "scratch.txt" if dirty_kind == "untracked" else "initial_program.py"
    assert expected_path in str(exc_info.value)
    assert "workspace.backend='copy'" in str(exc_info.value)
    assert not runtime.specs_dir.exists()


def test_freeze_spec_allows_ignored_untracked_git_source(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / ".gitignore").write_text("scratch.txt\n", encoding="utf-8")
    _initialize_source_repository(project)
    (project / "scratch.txt").write_text("temporary\n", encoding="utf-8")
    runtime = FileSearchRuntime(tmp_path / ".search")

    frozen = runtime.freeze_spec(
        _spec_with_workspace_backend(project, "git_worktree"),
        [project / "evaluator.py"],
    )

    assert frozen.spec.workspace.backend == "git_worktree"


def test_git_freeze_verifies_origin_and_first_candidate_before_search(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    counter = tmp_path / "verifier-count.txt"
    (project / "evaluator.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        f"counter = Path({str(counter)!r})\n"
        "count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        "if count == 2 and (Path.cwd().parent / 'c002').exists():\n"
        "    raise SystemExit('c002 existed before relocation verification')\n"
        "print(json.dumps({'combined_score': 3.5}))\n",
        encoding="utf-8",
    )
    _initialize_source_repository(project)
    runtime = FileSearchRuntime(tmp_path / ".search")

    frozen = runtime.freeze_spec(
        _spec_with_workspace_backend(project, "git_worktree"),
        [project / "evaluator.py"],
    )

    assert counter.read_text(encoding="utf-8") == "1"
    assert frozen.source_artifact_ref is not None
    assert frozen.origin_verification is not None
    assert frozen.origin_verification.phase == "origin"
    assert frozen.origin_verification.passed is True
    assert frozen.origin_verification.aggregate_score == 3.5

    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    run = runtime.search_run.state.read_run(run_id)

    assert [task.candidate_id for task in tasks] == ["c001", "c002"]
    assert all(
        task.workspace_base_revision == frozen.source_artifact_ref.id
        for task in tasks
    )
    assert counter.read_text(encoding="utf-8") == "2"
    assert run.workspace_bootstrap_verification is not None
    assert run.workspace_bootstrap_verification.phase == "workspace_relocation"
    assert run.workspace_bootstrap_verification.candidate_id == "c001"
    assert run.workspace_bootstrap_verification.passed is True
    assert run.workspace_bootstrap_verification.aggregate_score == 3.5
    assert all(not record.iterations for record in runtime.candidate_creation.state.read_candidates(run_id))
    monitor = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    assert monitor["run"]["source_artifact_ref"] == frozen.source_artifact_ref.model_dump(
        mode="json"
    )
    assert monitor["run"]["origin_verification"]["passed"] is True
    assert (
        monitor["run"]["workspace_bootstrap_verification"]["candidate_id"]
        == "c001"
    )

    report_path = runtime.report(run_id)
    report_text = report_path.read_text(encoding="utf-8")
    html_text = report_path.with_suffix(".html").read_text(encoding="utf-8")
    assert "## Bootstrap Verification" in report_text
    assert "`workspace_relocation`" in report_text
    assert "Bootstrap Verification" in html_text
    assert "workspace_relocation" in html_text


def test_git_freeze_rejects_failing_origin_verifier_before_spec_creation(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    _initialize_source_repository(project)
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="OriginVerifierFailed"):
        runtime.freeze_spec(
            _spec_with_workspace_backend(project, "git_worktree"),
            [project / "evaluator.py"],
        )

    assert not runtime.specs_dir.exists()
    assert list((runtime.root_dir / "bootstrap").glob("origin-*/*score*.log"))


def test_git_origin_verifier_may_write_ignored_cache(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (project / "evaluator.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "Path('cache').mkdir(exist_ok=True)\n"
        "Path('cache/result.txt').write_text('compiled')\n"
        "print(json.dumps({'combined_score': 1}))\n",
        encoding="utf-8",
    )
    _initialize_source_repository(project)
    runtime = FileSearchRuntime(tmp_path / ".search")

    frozen = runtime.freeze_spec(
        _spec_with_workspace_backend(project, "git_worktree"),
        [project / "evaluator.py"],
    )

    assert frozen.origin_verification is not None
    assert frozen.origin_verification.passed is True
    assert (project / "cache" / "result.txt").is_file()
    assert subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=project,
        text=True,
    ) == ""


def test_freeze_spec_copy_backend_allows_dirty_git_source(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    _initialize_source_repository(project)
    (project / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "scratch.txt").write_text("temporary\n", encoding="utf-8")
    runtime = FileSearchRuntime(tmp_path / ".search")

    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])

    assert frozen.spec.workspace.backend == "copy"
    assert frozen.spec.workspace.promotion_mode == "artifact_only"


def test_freeze_spec_git_worktree_keeps_plain_directory_support(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")

    frozen = runtime.freeze_spec(
        _spec_with_workspace_backend(project, "git_worktree"),
        [project / "evaluator.py"],
    )

    assert frozen.spec.workspace.backend == "git_worktree"


def test_freeze_spec_checks_the_whole_repository_for_nested_source(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    project = make_project(repository)
    (repository / "README.md").write_text("baseline\n", encoding="utf-8")
    _initialize_source_repository(repository)
    (repository / "README.md").write_text("dirty\n", encoding="utf-8")
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="README.md"):
        runtime.freeze_spec(
            _spec_with_workspace_backend(project, "git_worktree"),
            [project / "evaluator.py"],
        )


def test_create_run_rechecks_clean_git_source(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    _initialize_source_repository(project)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _spec_with_workspace_backend(project, "git_worktree"),
        [project / "evaluator.py"],
    )
    (project / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="DirtySourceWorkspace"):
        runtime.create_run(frozen.frozen_spec_id)

    assert not runtime.runs_dir.exists()


def test_create_run_rejects_clean_git_source_at_new_head(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    _initialize_source_repository(project)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _spec_with_workspace_backend(project, "git_worktree"),
        [project / "evaluator.py"],
    )
    (project / "initial_program.py").write_text("VALUE = 2\n", encoding="utf-8")
    git_commit_all(project, "new clean source head")

    with pytest.raises(RuntimeError, match="FrozenSourceChanged"):
        runtime.create_run(frozen.frozen_spec_id)

    assert not runtime.runs_dir.exists()


def test_git_freeze_rejects_verifier_ignored_by_outer_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    nested = repository / "case" / "workspace"
    nested.mkdir(parents=True)
    (nested / "initial_program.py").write_text("VALUE = 0\n", encoding="utf-8")
    (nested / "evaluator.py").write_text(
        "import json\nprint(json.dumps({'combined_score': 1}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=nested, check=True)
    git_commit_all(nested, "nested task")

    (repository / ".gitignore").write_text("case/workspace/\n", encoding="utf-8")
    (repository / "README.md").write_text("outer repository\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    git_commit_all(repository, "outer source")

    spec_data = spec_for(repository, max_parallel=2).model_dump(mode="json")
    spec_data["workspace"] = {"backend": "git_worktree"}
    spec_data["edit_surface"] = {
        "allow": ["case/workspace/initial_program.py"],
        "deny": ["case/workspace/evaluator.py"],
    }
    spec_data["process_verifiers"][0]["cwd"] = "case/workspace"
    runtime = FileSearchRuntime(tmp_path / ".search")
    with pytest.raises(ValueError, match="VerifierArtifactNotMaterialized"):
        runtime.freeze_spec(
            SearchSpec.model_validate(spec_data),
            [nested / "evaluator.py"],
        )

    assert not runtime.specs_dir.exists()


def test_git_relocation_rejects_nonignored_verifier_side_effect(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "if os.environ.get('GOAL_PLUS_VERIFIER_PHASE') == 'workspace_relocation':\n"
        "    Path('initial_program.py').write_text('VALUE = 99\\n')\n"
        "print(json.dumps({'combined_score': 1}))\n",
        encoding="utf-8",
    )
    _initialize_source_repository(project)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _spec_with_workspace_backend(project, "git_worktree"),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)

    with pytest.raises(RuntimeError, match="WorkspaceRelocationVerifierFailed"):
        runtime.start_batch(run_id, plan.plan_id)

    run = runtime.search_run.state.read_run(run_id)
    assert run.workspace_bootstrap_verification is not None
    failures = [
        result.failure_class
        for result in run.workspace_bootstrap_verification.verifier_results
        if not result.passed
    ]
    assert "VerifierWorkspaceSideEffect" in failures
    assert run.candidates_total == 1


def test_create_run_ignores_runtime_state_inside_source_repository(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    _initialize_source_repository(project)
    runtime = FileSearchRuntime(project / ".gp")
    frozen = runtime.freeze_spec(
        _spec_with_workspace_backend(project, "git_worktree"),
        [project / "evaluator.py"],
    )

    run_id = runtime.create_run(frozen.frozen_spec_id)

    assert runtime.status(run_id).run_id == run_id


@pytest.mark.parametrize("worker_host", ["codex", "pi-rpc"])
def test_freeze_spec_rejects_thinkthread_before_verifier_preflight(
    tmp_path: Path,
    worker_host: str,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = spec_for(project).model_dump(mode="json")
    spec_data["strategy"]["worker_host"] = worker_host
    spec_data["workspace"] = {"backend": "thinkthread"}
    with pytest.raises(
        UnsupportedHostCapability,
        match=rf"{worker_host} worker_host does not support workspace backend 'thinkthread'",
    ) as exc_info:
        runtime.freeze_spec(
            SearchSpec.model_validate(spec_data),
            [project / "evaluator.py"],
        )

    assert "supported backends: copy, git_worktree" in str(exc_info.value)
    assert "before Search run creation" in str(exc_info.value)
    assert not runtime.specs_dir.exists()
    assert not runtime.runs_dir.exists()


def test_create_run_rechecks_existing_thinkthread_frozen_spec(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])
    frozen_path = runtime.specs_dir / frozen.frozen_spec_id / "frozen_spec.json"
    payload = load_json(frozen_path)
    payload["spec"]["workspace"] = {"backend": "thinkthread"}
    write_json(frozen_path, payload)

    with pytest.raises(
        UnsupportedHostCapability,
        match="codex worker_host does not support workspace backend 'thinkthread'",
    ):
        runtime.create_run(frozen.frozen_spec_id)

    assert not runtime.runs_dir.exists()


def test_freeze_spec_accepts_enabled_thinkthread_host_without_creating_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = spec_for(project).model_dump(mode="json")
    spec_data["strategy"]["worker_host"] = "thinkthread"
    spec_data["strategy"]["worker_launch"] = {"agent_profile": "pi"}
    spec_data["workspace"] = {"backend": "thinkthread"}

    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )

    assert frozen.spec.strategy.worker_host == "thinkthread"
    assert frozen.spec.workspace.backend == "thinkthread"
    assert frozen.spec.strategy.worker_launch is not None
    assert frozen.spec.strategy.worker_launch.agent_profile == "pi"
    assert runtime.specs_dir.exists()
    assert not runtime.runs_dir.exists()


def test_freeze_spec_rejects_verifier_workspace_side_effect_without_mutating_source(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "output = Path('.goal-plus-verifiers/generated.bin')\n"
        "output.parent.mkdir(parents=True, exist_ok=True)\n"
        "output.write_text('compiled', encoding='utf-8')\n"
        "print(json.dumps({'combined_score': 1.0}))\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="VerifierWorkspaceSideEffect") as exc_info:
        runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])

    assert ".goal-plus-verifiers/generated.bin" in str(exc_info.value)
    assert "GOAL_PLUS_VERIFIER_TMPDIR" in str(exc_info.value)
    assert "concurrently" in str(exc_info.value)
    assert not (project / ".goal-plus-verifiers").exists()
    assert not runtime.specs_dir.exists()


def test_freeze_spec_provides_per_invocation_verifier_temp_directory(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "names = ('TMPDIR', 'TMP', 'TEMP', 'GOAL_PLUS_VERIFIER_TMPDIR')\n"
        "paths = {os.environ[name] for name in names}\n"
        "assert len(paths) == 1\n"
        "tmp = Path(paths.pop())\n"
        "assert tmp.is_dir()\n"
        "assert os.environ['GOAL_PLUS_VERIFIER_PHASE'] == 'freeze_preflight'\n"
        "tmp.joinpath('compiled.bin').write_text('ok', encoding='utf-8')\n"
        "print(json.dumps({'combined_score': 1.0}))\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])

    assert frozen.frozen_spec_id.startswith("spec_")
    assert not (project / "compiled.bin").exists()


def test_freeze_spec_rejects_verifier_outputs_in_workspace_tmp(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "output = Path('.tmp/verifier-output.txt')\n"
        "output.parent.mkdir(parents=True, exist_ok=True)\n"
        "output.write_text('not worker scratch', encoding='utf-8')\n"
        "print(json.dumps({'combined_score': 1.0}))\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="VerifierWorkspaceSideEffect") as exc_info:
        runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])

    assert ".tmp/verifier-output.txt" in str(exc_info.value)
    assert not (project / ".tmp").exists()


def test_freeze_preflight_overrides_verifier_phase_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "import os\n"
        "phase = os.environ.get('GOAL_PLUS_VERIFIER_PHASE')\n"
        "assert phase == 'freeze_preflight', phase\n"
        "print(json.dumps({'combined_score': 1.0, 'phase': phase}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GOAL_PLUS_VERIFIER_PHASE", "caller_value")
    runtime = FileSearchRuntime(tmp_path / ".search")

    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])

    assert frozen.frozen_spec_id


def test_freeze_preflight_failure_includes_bounded_output_tails(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "import sys\n"
        "print(json.dumps({'status': 'error', 'error': 'baseline missing'}))\n"
        "print('device unavailable', file=sys.stderr)\n"
        "raise SystemExit(20)\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="failed during freeze preflight") as exc_info:
        runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])

    message = str(exc_info.value)
    assert "Stdout tail:" in message
    assert '"error": "baseline missing"' in message
    assert "Stderr tail: device unavailable" in message


def test_freeze_spec_rejects_plain_text_ranking_score(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "print('Score = 18941966307')\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="emitted no finite numeric metric") as exc_info:
        runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])

    assert '{"combined_score":123.0}' in str(exc_info.value)
    assert "expected_outputs lists artifact paths only" in str(exc_info.value)
    assert not runtime.specs_dir.exists()


@pytest.mark.parametrize(
    "metric_value",
    ["'18941966307'", "True", "float('nan')", "float('inf')"],
)
def test_freeze_spec_rejects_non_numeric_or_non_finite_ranking_score(
    tmp_path: Path,
    metric_value: str,
) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        f"print(json.dumps({{'combined_score': {metric_value}}}))\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="emitted no finite numeric metric"):
        runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])


def test_null_verifier_error_is_accepted_by_preflight_and_runtime(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "print(json.dumps({'combined_score': 7.0, 'error': None}))\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    report = runtime.run_verifier(run_id, task.candidate_id)

    assert report.process_passed is True
    assert report.aggregate_score == 7.0
    assert report.verifier_results[0].metrics["error"] is None


def test_freeze_spec_rejects_non_null_verifier_error(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "print(json.dumps({'combined_score': 7.0, 'error': 'broken evaluator'}))\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="reported an error.*broken evaluator"):
        runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])

    assert not runtime.specs_dir.exists()


def test_runtime_rejects_non_null_verifier_error_after_clean_preflight(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "from initial_program import VALUE\n"
        "error = None if VALUE == 0 else 'candidate evaluation failed'\n"
        "print(json.dumps({'combined_score': 7.0, 'error': error}))\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    report = runtime.run_verifier(run_id, task.candidate_id)

    assert report.process_passed is False
    assert report.aggregate_score == 0.0
    assert report.attempt_id is not None
    assert report.attempt_id.startswith("attempt_")
    assert report.attempt_iteration == 1
    assert report.attempt_artifact_ref is not None
    assert report.verifier_results[0].failure_class == "VerifierCommandFailed"
    assert report.verifier_results[0].metrics["error"] == "candidate evaluation failed"
    record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert len(record.iterations) == 1
    assert record.iterations[0].metrics["_candidate_attempt"]["id"] == report.attempt_id


def test_visible_verifier_failure_returns_diagnostics_and_preserves_logs(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json, sys\n"
        "from initial_program import VALUE\n"
        "if VALUE == 0:\n"
        "    print(json.dumps({'combined_score': 0.0}))\n"
        "else:\n"
        "    print('x' * 5000 + f'-stdout-value-{VALUE}')\n"
        "    print(f'stderr-value-{VALUE}', file=sys.stderr)\n"
        "    raise SystemExit(1)\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    assert task.workspace_creation_timing is not None
    assert task.workspace_creation_timing.outcome == "succeeded"
    assert task.workspace_creation_timing.duration_seconds >= 0

    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    first_report = runtime.run_verifier(run_id, task.candidate_id)
    first_result = first_report.verifier_results[0]
    first_log = Path(first_result.log_path)

    assert first_result.failure_class == "VerifierCommandFailed"
    assert len(first_result.metrics["stdout_tail"]) == 4000
    assert first_result.metrics["stdout_tail"].endswith("-stdout-value-1")
    assert first_result.metrics["stderr_tail"] == "stderr-value-1"
    assert first_log.name.startswith("iteration-0001-score-")
    assert "-stdout-value-1" in first_log.read_text(encoding="utf-8")

    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 22\n",
        encoding="utf-8",
    )
    second_report = runtime.run_verifier(run_id, task.candidate_id)
    second_result = second_report.verifier_results[0]
    second_log = Path(second_result.log_path)

    assert second_log != first_log
    assert second_log.name.startswith("iteration-0002-score-")
    assert "-stdout-value-22" in second_log.read_text(encoding="utf-8")
    assert "-stdout-value-1" in first_log.read_text(encoding="utf-8")
    iterations = runtime.list_iterations(
        run_id,
        task.candidate_id,
    )
    assert [iteration["log_paths"] for iteration in iterations] == [
        [str(first_log)],
        [str(second_log)],
    ]


@pytest.mark.parametrize("feedback_policy", ["summary_only", "final_only"])
def test_non_visible_feedback_policy_keeps_diagnostics_in_runtime_log(
    tmp_path: Path,
    feedback_policy: str,
) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json, sys\n"
        "from initial_program import VALUE\n"
        "if VALUE == 0:\n"
        "    print(json.dumps({'combined_score': 0.0}))\n"
        "else:\n"
        "    print('private stdout')\n"
        "    print('private stderr', file=sys.stderr)\n"
        "    raise SystemExit(1)\n",
        encoding="utf-8",
    )
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["process_verifiers"][0]["feedback_policy"] = feedback_policy
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    report = runtime.run_verifier(run_id, task.candidate_id)

    result = report.verifier_results[0]
    assert result.failure_class == "VerifierCommandFailed"
    assert "stdout_tail" not in result.metrics
    assert "stderr_tail" not in result.metrics
    log_text = Path(result.log_path).read_text(encoding="utf-8")
    assert "private stdout" in log_text
    assert "private stderr" in log_text


@pytest.mark.parametrize("runtime_dir", [".gp", ".search"])
def test_freeze_spec_rejects_verifier_artifact_under_runtime_root(
    tmp_path: Path,
    runtime_dir: str,
) -> None:
    project = make_project(tmp_path)
    verifier = project / runtime_dir / "verifiers" / "score.sh"
    verifier.parent.mkdir(parents=True)
    verifier.write_text(
        "#!/usr/bin/env bash\nprintf '{\"combined_score\": 1}\\n'\n",
        encoding="utf-8",
    )
    spec_data = spec_for(project).model_dump(mode="json")
    spec_data["process_verifiers"][0]["command"] = [
        "bash",
        f"{runtime_dir}/verifiers/score.sh",
    ]
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(
        ValueError,
        match=f"ignored Goal Plus runtime directory '{runtime_dir}'",
    ):
        runtime.freeze_spec(SearchSpec.model_validate(spec_data), [verifier])


def test_freeze_spec_rejects_verifier_artifact_outside_source_path(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    verifier = tmp_path / "external-verifier.py"
    verifier.write_text(
        "import json\nprint(json.dumps({'combined_score': 1}))\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="outside source_path"):
        runtime.freeze_spec(spec_for(project), [verifier])


def test_source_owned_verifier_is_present_in_git_worktree_candidate(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    verifier = project / ".goal-plus-verifiers" / "score.sh"
    verifier.parent.mkdir()
    verifier.write_text(
        "#!/usr/bin/env bash\nprintf '{\"combined_score\": 1}\\n'\n",
        encoding="utf-8",
    )
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["workspace"] = {"backend": "git_worktree"}
    spec_data["process_verifiers"][0]["command"] = [
        "bash",
        ".goal-plus-verifiers/score.sh",
    ]
    spec_data["edit_surface"]["deny"].append(".goal-plus-verifiers/")
    runtime = FileSearchRuntime(tmp_path / ".search")

    frozen = runtime.freeze_spec(SearchSpec.model_validate(spec_data), [verifier])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    candidate_verifier = task.workspace / ".goal-plus-verifiers" / "score.sh"

    assert candidate_verifier.exists()
    assert sha256_file(candidate_verifier) == frozen.verifier_hashes[
        ".goal-plus-verifiers/score.sh"
    ]


def test_create_run_reuses_frozen_verifier_with_current_source_baseline(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    first_run_id = runtime.create_run(frozen.frozen_spec_id)
    project.joinpath("initial_program.py").write_text(
        "VALUE = 7\n",
        encoding="utf-8",
    )

    second_run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(second_run_id, requested_k=1)
    task = runtime.start_batch(second_run_id, plan.plan_id)[0]

    assert second_run_id != first_run_id
    assert task.workspace.joinpath("initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 7\n"
    assert sha256_file(task.workspace / "evaluator.py") == frozen.verifier_hashes[
        "evaluator.py"
    ]


def test_runtime_marks_missing_ranking_metric_as_failure(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "if 'VALUE = 0' in Path('initial_program.py').read_text():\n"
        "    print(json.dumps({'combined_score': 0}))\n"
        "else:\n"
        "    print('Score = 42')\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=1), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    report = runtime.run_verifier(run_id, task.candidate_id)

    result = report.verifier_results[0]
    assert report.process_passed is False
    assert report.aggregate_score == 0.0
    assert result.failure_class == "MissingNumericMetric"
    assert result.metrics["expected_metric_name"] == "combined_score"
    assert result.metrics["stdout_tail"] == "Score = 42"


def test_select_chooses_highest_json_ranking_metric(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "from initial_program import VALUE\n"
        "print(json.dumps({'combined_score': VALUE}))\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=3), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=3)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    for task, score in zip(tasks, [10, 20, 30], strict=True):
        task.workspace.joinpath("initial_program.py").write_text(
            f"VALUE = {score}\n",
            encoding="utf-8",
        )
        report = runtime.run_verifier(run_id, task.candidate_id)
        assert report.aggregate_score == score

    selection = runtime.select(run_id)

    assert selection["selected_candidate_id"] == "c003"
    assert selection["selected_score"] == 30.0


def test_select_records_recoverable_selection_blocked_state(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=2),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    first_plan = runtime.plan_next(run_id, requested_k=1)
    runtime.start_batch(run_id, first_plan.plan_id)

    with pytest.raises(RuntimeError, match="no verified candidates"):
        runtime.select(run_id)

    blocked = runtime.search_run.state.read_run(run_id)
    assert blocked.state == "selection_blocked"
    assert blocked.budget_used["selection_blocked_reason"] == (
        "no verifier-backed candidate iteration is eligible for selection"
    )
    recovery = runtime.redispatch_candidate(run_id, "c001")
    assert recovery.candidate_id == "c001"


def test_load_legacy_frozen_spec_without_workspace_uses_copy_backend(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])
    frozen_path = runtime.root_dir / "specs" / frozen.frozen_spec_id / "frozen_spec.json"
    frozen_data = load_json(frozen_path)
    frozen_data["spec"].pop("workspace")
    write_json(frozen_path, frozen_data)

    loaded = runtime.search_run.state.read_frozen_spec(frozen.frozen_spec_id)

    assert loaded.spec.workspace.backend == "copy"


def test_runtime_defaults_to_git_worktree_workspace(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data.pop("workspace")
    spec = SearchSpec.model_validate(spec_data)

    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    assert task.workspace_backend == "git_worktree"
    assert task.workspace_branch == f"gp/{run_id}/c001"
    common_dir = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=task.workspace,
        text=True,
    ).strip()
    assert (task.workspace / common_dir).resolve() == (
        runtime.search_run.state.run_dir(run_id) / "workspace-repository" / ".git"
    ).resolve()


@pytest.mark.codex
def test_git_tracked_task_skills_are_discovered_in_each_candidate_worktree(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    skill_root = project / ".agents" / "skills" / "review"
    (skill_root / "references").mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review this candidate.\n---\n"
        "# Review\n\nRead references/checks.md.\n",
        encoding="utf-8",
    )
    (skill_root / "references" / "checks.md").write_text(
        "# Checks\n\nInspect the candidate worktree only.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    git_commit_all(project, "source baseline")
    git_exclude = project / ".git" / "info" / "exclude"
    git_exclude.write_text(
        f"{git_exclude.read_text(encoding='utf-8')}\n.agents/skills/untracked/\n",
        encoding="utf-8",
    )
    untracked_skill = project / ".agents" / "skills" / "untracked" / "SKILL.md"
    untracked_skill.parent.mkdir(parents=True)
    untracked_skill.write_text("# Untracked\n", encoding="utf-8")
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = spec_with_host(
        project,
        "codex",
        strategy_name="random",
        max_parallel=2,
    ).model_dump(mode="json")
    spec_data["workspace"] = {"backend": "git_worktree"}
    spec = SearchSpec.model_validate(spec_data)
    frozen = runtime.freeze_spec(
        spec,
        [project / "evaluator.py"],
    )

    assert frozen.task_skill_paths == [".agents/skills/review/SKILL.md"]
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    assert len(tasks) == 2
    assert len({task.workspace for task in tasks}) == 2
    assert len({task.workspace_branch for task in tasks}) == 2
    for task in tasks:
        expected_skill = task.workspace / ".agents" / "skills" / "review" / "SKILL.md"
        assert task.task_skill_paths == [expected_skill]
        assert expected_skill.is_file()
        assert (expected_skill.parent / "references" / "checks.md").is_file()

        session = runtime.start_agent_session(run_id, task.candidate_id)
        context = runtime.get_agent_context(session.agent_session_id)
        assert context["candidate_task"]["workspace"] == str(task.workspace)
        assert context["candidate_task"]["task_skill_paths"] == [str(expected_skill)]
        assert any(
            "host launch 不保证切换 cwd" in instruction
            for instruction in task.instructions
        )
        assert all("Codex" not in instruction for instruction in task.instructions)
        assert "candidate_task" in session.launch["message"]
        assert "task_local_skills_visible: review=" in session.launch["message"]
        assert str(expected_skill) in session.launch["message"]
        assert "untracked" not in session.launch["message"]


def test_no_git_tracked_task_skills_is_reported_explicitly(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    assert frozen.task_skill_paths == []
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    assert task.task_skill_paths == []
    assert any("未发现 Git-tracked" in item for item in task.instructions)
    assert "task_local_skills_visible: none" in session.launch["message"]


def test_freeze_rejects_git_tracked_symlink_skill_entrypoint(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    skill_root = project / ".agents" / "skills" / "review"
    skill_root.mkdir(parents=True)
    skill_path = skill_root / "SKILL.md"
    skill_path.symlink_to(project / "initial_program.py")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "add", ".agents/skills/review/SKILL.md"],
        cwd=project,
        check=True,
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="cannot be a symlink"):
        runtime.freeze_spec(
            spec_for(project, max_parallel=1),
            [project / "evaluator.py"],
        )


def test_freeze_rejects_symlink_inside_git_tracked_skill(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    skill_root = project / ".agents" / "skills" / "review"
    references = skill_root / "references"
    references.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Review\n", encoding="utf-8")
    (references / "checks.md").symlink_to(project / "initial_program.py")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "add", ".agents/skills/review"],
        cwd=project,
        check=True,
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="bundle cannot contain a symlink"):
        runtime.freeze_spec(
            spec_for(project, max_parallel=1),
            [project / "evaluator.py"],
        )


def test_freeze_rejects_untracked_file_in_git_tracked_skill(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    skill_root = project / ".agents" / "skills" / "review"
    (skill_root / "references").mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Review\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "add", ".agents/skills/review/SKILL.md"],
        cwd=project,
        check=True,
    )
    (skill_root / "references" / "checks.md").write_text(
        "# Untracked checks\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(ValueError, match="files not tracked by Git.*checks.md"):
        runtime.freeze_spec(
            spec_for(project, max_parallel=1),
            [project / "evaluator.py"],
        )


def test_freeze_reports_git_discovery_failure_when_skill_directory_exists(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / ".git").write_text("gitdir: missing\n", encoding="utf-8")
    skill_root = project / ".agents" / "skills" / "review"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Review\n", encoding="utf-8")
    runtime = FileSearchRuntime(tmp_path / ".search")

    with pytest.raises(RuntimeError, match="Cannot inspect task-local Skills.*git ls-files"):
        runtime.freeze_spec(
            spec_for(project, max_parallel=1),
            [project / "evaluator.py"],
        )


def test_freeze_spec_normalizes_verifier_cwd_equal_to_source_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    project = repo / "examples" / "nested-project" / "workspace-template"
    project.mkdir(parents=True)
    (project / "initial_program.py").write_text("VALUE = 0\n", encoding="utf-8")
    (project / "evaluator.py").write_text(
        "import json\nprint(json.dumps({'combined_score': 1.0}))\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)

    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["source_path"] = "examples/nested-project/workspace-template"
    spec_data["process_verifiers"][0]["cwd"] = (
        "examples/nested-project/workspace-template"
    )
    spec_data["promotion_verifiers"] = [
        {
            "name": "promotion",
            "role": "promotion_gate",
            "command": ["python", "evaluator.py"],
            "cwd": "examples/nested-project/workspace-template",
        }
    ]
    runtime = FileSearchRuntime(tmp_path / ".search")

    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )

    assert frozen.spec.process_verifiers[0].cwd == "."
    assert frozen.spec.promotion_verifiers[0].cwd == "."


def test_plan_next_and_start_batch_record_plan_metadata(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=2), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)

    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    assert plan.strategy.name == "random"
    assert plan.strategy.orchestration_mode == "parallel_loops"
    assert plan.planned_k == 2
    assert [task.candidate_id for task in tasks] == ["c001", "c002"]
    assert tasks[0].plan_id == "plan_001"
    assert tasks[0].proposal.intent == "独立候选 c001"  # type: ignore[union-attr]
    assert (tasks[0].workspace / ".tmp").is_dir()

    saved_plan = runtime.search_run.state.read_plan(run_id, "plan_001")
    assert saved_plan.status == "started"
    assert saved_plan.started_candidate_ids == ["c001", "c002"]


def test_candidate_workspace_has_isolated_git_baseline_under_ignored_parent(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=parent, check=True)
    (parent / ".gitignore").write_text(".tmp/\n", encoding="utf-8")

    project = make_project(parent)
    runtime = FileSearchRuntime(parent / ".tmp" / ".search")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=1), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=task.workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(root) == task.workspace

    (task.workspace / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")

    status = subprocess.run(
        ["git", "status", "--short", "initial_program.py"],
        cwd=task.workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "diff", "--", "initial_program.py"],
        cwd=task.workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert status.startswith("M ")
    assert "VALUE = 1" in diff
    assert runtime.artifacts.workspace.detect_changed_files(project, task.workspace) == ["initial_program.py"]


def test_worker_task_exposes_host_and_edit_contract(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_agent_type": "search_candidate_agent",
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    assert plan.worker_policy["host"] == "codex"
    assert plan.worker_policy["inner_agent"] == "autoresearch"
    assert plan.worker_policy["inner_agent_binding"]["inner_agent_id"] == (
        "autoresearch"
    )
    assert plan.worker_policy["legacy_worker_agent_type"] == (
        "search_candidate_agent"
    )
    assert "worker_mode" not in tasks[0].strategy_metadata
    assert tasks[0].workspace_access.native_cwd == tasks[0].workspace
    assert tasks[0].workspace_access.bootstrap_required is True
    assert tasks[0].workspace_access.confinement == "soft_path"
    assert tasks[0].allowed_files == ["initial_program.py"]
    assert tasks[0].denied_files == ["evaluator.py", "config.yaml"]

    (tasks[0].workspace / ".tmp" / "handoff.json").write_text(
        '{"summary": "resume here"}\n',
        encoding="utf-8",
    )
    assert runtime.artifacts.workspace.detect_changed_files(project, tasks[0].workspace) == []


def test_git_common_inner_agent_uses_one_attempt_per_dispatch_contract(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "inner_agent": "common",
            "worker_budget": {
                "max_runtime_seconds": 60,
                "max_turns": 8,
            },
        },
        max_parallel=1,
    )

    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    assert frozen.spec.strategy.inner_agent == "common"
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)

    instructions = "\n".join(task.instructions)
    assert plan.worker_policy["inner_agent"] == "common"
    assert (
        plan.worker_policy["inner_agent_binding"]["workspace_ledger_projection"]
        == "none"
    )
    assert not task.workspace.joinpath("results.tsv").exists()
    assert "本次 host dispatch 只完成一个有界 attempt" in instructions
    assert "调用一次 search_run_verifier" in instructions
    assert "不要自行开始下一轮" in instructions
    assert "不要仅因一次失败或同分而停止" not in instructions
    assert "普通 Search 候选 agent" in session.launch["prompt"]
    assert "调用一次\n`search_run_verifier`" in session.launch["prompt"]
    assert "worker_agent_type" not in session.launch

    context = runtime.get_agent_context(session.agent_session_id)
    assert context["inner_agent"] == "common"
    assert context["result_ledger_source"] == "gp"
    assert context["workspace_ledger_projection"] == "none"
    assert context["results_tsv"] is None

    program = task.workspace / "initial_program.py"
    program.write_text(
        program.read_text(encoding="utf-8") + "\n# common agent attempt\n",
        encoding="utf-8",
    )
    report = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="common agent checkpoint",
    )
    assert report.process_passed is True
    assert not task.workspace.joinpath("results.tsv").exists()

    record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert len(record.results_ledger) == 1
    assert record.results_ledger_git_head is None
    assert record.iterations[0].ledger_git_head is None
    assert record.iterations[0].workspace_artifact_after_settlement is not None
    assert (
        record.iterations[0].workspace_git_head_after_settlement
        == record.iterations[0].workspace_artifact_after_settlement.id
    )
    assert subprocess.check_output(
        ["git", "log", "-1", "--format=%s"],
        cwd=task.workspace,
        text=True,
    ).strip() == f"search verifier iteration {task.candidate_id}:1"

    monitor = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    candidate_monitor = monitor["candidates"][task.candidate_id]
    assert candidate_monitor["result_ledger_source"] == "gp"
    assert candidate_monitor["result_count"] == 1
    assert candidate_monitor["workspace_ledger_projection"] == "none"
    assert candidate_monitor["results_tsv"]["path"] is None
    assert candidate_monitor["results_tsv"]["exists"] is False
    assert candidate_monitor["results_tsv"]["row_count"] is None

    generated_report = runtime.report(run_id).read_text(encoding="utf-8")
    assert ".gp canonical (1 rows)" in generated_report
    assert "missing results.tsv projection" not in generated_report
    runtime.bind_agent_handle(
        session.agent_session_id,
        {
            "host": "pi-rpc",
            "external_id": session.agent_session_id,
            "metadata": {"event_log": "/tmp/pi-rpc-common.jsonl"},
        },
    )

    continued = runtime.continue_agent_session(session.agent_session_id)

    assert continued.agent_session_id == session.agent_session_id
    assert "完成一个新的有界尝试" in continued.launch["prompt"]
    assert "调用一次 search_run_verifier 后返回" in continued.launch["prompt"]
    assert "不要自行开始下一轮" in continued.launch["prompt"]

    original_config = task.workspace.joinpath("config.yaml").read_text(
        encoding="utf-8"
    )
    task.workspace.joinpath("config.yaml").write_text(
        "name: forbidden common change\n",
        encoding="utf-8",
    )
    failed = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="common regression is restored",
    )
    assert failed.process_passed is False
    assert failed.disposition == "failure"
    assert task.workspace.joinpath("config.yaml").read_text(
        encoding="utf-8"
    ) == original_config
    assert not task.workspace.joinpath("results.tsv").exists()

    restored = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert len(restored.results_ledger) == 2
    assert restored.results_ledger_git_head is None
    assert restored.iterations[-1].ledger_git_head is None
    assert restored.iterations[-1].restored_to_artifact_ref is not None
    assert (
        restored.iterations[-1].workspace_artifact_after_settlement
        == restored.results_ledger_artifact_ref
    )


def test_inner_agent_binding_fails_closed_when_prompt_asset_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "inner_agent": "common",
            "worker_budget": {
                "max_runtime_seconds": 60,
                "max_turns": 8,
            },
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    monkeypatch.setattr("goal_plus.infrastructure.artifacts.candidate_launch.host_assets_root", lambda: tmp_path / "missing")

    with pytest.raises(RuntimeError, match="InnerAgentAssetUnavailable"):
        runtime.start_agent_session(run_id, task.candidate_id)


@pytest.mark.parametrize(
    ("mode", "supplemental", "threshold", "adaptive"),
    [
        ("manual", True, True, False),
        ("auto", False, False, True),
        ("independent", False, False, False),
    ],
)
def test_worker_instructions_match_enabled_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    supplemental: bool,
    threshold: bool,
    adaptive: bool,
) -> None:
    monkeypatch.setenv(
        "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED",
        "1" if supplemental else "0",
    )
    project = make_project(tmp_path)
    strategy = {"name": "random", "config": {"global_evidence_mode": mode}}
    if adaptive:
        strategy.update(
                {
                    "orchestration_mode": "adaptive_search",
                    "worker_host": "pi-rpc",
                    "worker_budget": {"max_runtime_seconds": 60},
                    "search_scheduler": {
                    "host": "pi-rpc",
                    "model": "scheduler-model",
                    "reasoning_effort": "low",
                    "timeout_seconds": 60,
                    "reward": "evidence_llm_value/v3",
                    "allocation": "value_guided_replace/v2",
                },
            }
        )
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["strategy"] = strategy
    if adaptive:
        data["workspace"]["backend"] = "git_worktree"
    data["constraints"] = {"success_threshold": 1.0} if threshold else {}
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    instructions = "\n".join(task.instructions)

    assert ("search_get_global_evidence" in instructions) is (mode == "manual")
    assert ("global_evidence_snapshot" in instructions) is (mode == "auto")
    assert ("search_get_evidence_detail" in instructions) is supplemental
    assert ("success_threshold" in instructions) is threshold
    assert ("allocation_decision" in instructions) is adaptive


def test_promote_requires_search_runtime_selection(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, workspace = create_candidate(runtime, project)
    (workspace / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")
    report = runtime.run_verifier(run_id, candidate_id)
    assert report.process_passed is True
    unselected = _guarded_runtime_state(runtime, run_id, candidate_id)

    with pytest.raises(RuntimeError, match="search_select"):
        runtime.promote(run_id, candidate_id)
    assert _guarded_runtime_state(runtime, run_id, candidate_id) == unselected

    selected = runtime.select(run_id)
    assert selected["selected_candidate_id"] == candidate_id
    ready_to_promote = _guarded_runtime_state(runtime, run_id, candidate_id)

    with pytest.raises(RuntimeError, match="ready_to_promote"):
        runtime.plan_next(run_id, requested_k=1)
    with pytest.raises(RuntimeError, match="ready_to_promote"):
        runtime.start_agent_session(run_id, candidate_id)
    with pytest.raises(RuntimeError, match="ready_to_promote"):
        runtime.run_verifier(run_id, candidate_id)
    assert _guarded_runtime_state(runtime, run_id, candidate_id) == ready_to_promote

    assert runtime.promote(run_id, candidate_id).exists()
    promoted = _guarded_runtime_state(runtime, run_id, candidate_id)
    with pytest.raises(RuntimeError, match="promoted"):
        runtime.plan_next(run_id, requested_k=1)
    with pytest.raises(RuntimeError, match="promoted"):
        runtime.start_agent_session(run_id, candidate_id)
    with pytest.raises(RuntimeError, match="promoted"):
        runtime.run_verifier(run_id, candidate_id)
    with pytest.raises(RuntimeError, match="promoted"):
        runtime.select(run_id)
    assert _guarded_runtime_state(runtime, run_id, candidate_id) == promoted


def test_promotion_verifier_is_selected_parent_only(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["promotion_verifiers"] = [
        {
            "name": "promotion",
            "role": "promotion_gate",
            "command": ["python", "evaluator.py"],
            "timeout_seconds": 30,
        }
    ]
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)

    with pytest.raises(ValueError, match="scope must be"):
        runtime.run_verifier(
            run_id,
            task.candidate_id,
            scope="fused vector recurrence",  # type: ignore[arg-type]
            agent_session_id=session.agent_session_id,
        )
    untouched = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert untouched.iterations == []
    assert untouched.promotion_report is None

    runtime.run_verifier(run_id, task.candidate_id)

    with pytest.raises(RuntimeError, match="selected by search_select"):
        runtime.run_verifier(run_id, task.candidate_id, scope="promotion")

    runtime.select(run_id)
    with pytest.raises(PermissionError, match="parent-owned"):
        runtime.run_verifier(
            run_id,
            task.candidate_id,
            scope="promotion",
            agent_session_id=session.agent_session_id,
        )

    report = runtime.run_verifier(run_id, task.candidate_id, scope="promotion")
    assert report.promotion_passed is True


def test_promote_patch_round_trips_file_without_trailing_newline(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, workspace = create_candidate(runtime, project)
    workspace.joinpath("initial_program.py").write_bytes(b"VALUE = 1")
    assert runtime.run_verifier(run_id, candidate_id).process_passed is True
    runtime.select(run_id)
    report_path = runtime.report(run_id)
    html_report_path = report_path.with_suffix(".html")
    assert "[report.html](report.html)" in report_path.read_text(encoding="utf-8")
    assert html_report_path.is_file()
    assert "- State: `ready_to_promote`" in report_path.read_text(encoding="utf-8")

    patch_path = runtime.promote(run_id, candidate_id)
    assert "- State: `promoted`" in report_path.read_text(encoding="utf-8")
    assert ">promoted</span>" in html_report_path.read_text(encoding="utf-8")
    apply_target = tmp_path / "apply-target"
    copy_source_tree(project, apply_target)
    subprocess.run(
        ["git", "apply", "--check", str(patch_path)],
        cwd=apply_target,
        check=True,
    )
    subprocess.run(["git", "apply", str(patch_path)], cwd=apply_target, check=True)

    assert apply_target.joinpath("initial_program.py").read_bytes() == b"VALUE = 1"


def test_promote_returns_apply_coordinates_for_nested_source_path(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    project = make_project(repository)
    git_commit_all(repository, "source baseline")

    runtime = FileSearchRuntime(repository / ".gp")
    run_id, candidate_id, workspace = create_candidate(runtime, project)
    workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    assert runtime.run_verifier(run_id, candidate_id).process_passed is True
    runtime.select(run_id)

    promoted = SearchTools(runtime).search_promote(run_id, candidate_id)

    assert promoted["source_path"] == str(project.resolve())
    assert promoted["apply_cwd"] == str(repository.resolve())
    assert promoted["apply_directory"] == "project"
    apply_args = [
        f"--directory={promoted['apply_directory']}",
        promoted["artifact_path"],
    ]
    subprocess.run(
        ["git", "-C", promoted["apply_cwd"], "apply", "--check", *apply_args],
        check=True,
    )
    subprocess.run(
        ["git", "-C", promoted["apply_cwd"], "apply", *apply_args],
        check=True,
    )
    assert project.joinpath("initial_program.py").read_text(encoding="utf-8") == (
        "VALUE = 1\n"
    )


def test_promote_runs_promotion_verifiers_and_keeps_failed_run_ready(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / "promotion.py").write_text(
        "import json\n"
        "import os\n"
        "assert os.environ.get('GOAL_PLUS_VERIFIER_PHASE') == 'promotion'\n"
        "print(json.dumps({'accepted': False}))\n"
        "raise SystemExit(3)\n",
        encoding="utf-8",
    )
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["promotion_verifiers"] = [
        {
            "name": "full_acceptance",
            "role": "promotion_gate",
            "command": ["python", "promotion.py"],
            "timeout_seconds": 30,
        }
    ]
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py", project / "promotion.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    runtime.run_verifier(run_id, task.candidate_id)
    runtime.select(run_id)
    before = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    process_score = before.score_report.aggregate_score  # type: ignore[union-attr]
    process_iterations = len(before.iterations)

    with pytest.raises(RuntimeError, match="fresh passing promotion evidence"):
        runtime.promote(run_id, task.candidate_id)

    run = runtime.search_run.state.read_run(run_id)
    record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert run.state == "ready_to_promote"
    assert record.score_report is not None
    assert record.score_report.aggregate_score == process_score
    assert len(record.iterations) == process_iterations
    assert record.promotion_report is not None
    assert record.promotion_report.promotion_passed is False
    assert record.promotion_evidence is not None
    assert record.promotion_evidence.passed is False
    assert not runtime.search_run.state.run_dir(run_id).joinpath(
        "promotion", f"{task.candidate_id}.patch"
    ).exists()


def test_promote_reruns_and_binds_fresh_promotion_evidence(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    counter_path = tmp_path / "promotion-count.txt"
    (project / "promotion.py").write_text(
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "assert os.environ.get('GOAL_PLUS_VERIFIER_PHASE') == 'promotion'\n"
        f"counter = Path({str(counter_path)!r})\n"
        "count = int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(count + 1))\n"
        "print(json.dumps({'accepted': True}))\n",
        encoding="utf-8",
    )
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["promotion_verifiers"] = [
        {
            "name": "full_acceptance",
            "role": "promotion_gate",
            "command": ["python", "promotion.py"],
            "timeout_seconds": 30,
        }
    ]
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py", project / "promotion.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    runtime.run_verifier(run_id, task.candidate_id)
    runtime.select(run_id)
    run_path = runtime.search_run.state.run_dir(run_id) / "run.json"
    legacy_run = load_json(run_path)
    legacy_run.pop("selected_artifact_hash")
    write_json(run_path, legacy_run)
    candidate_path = (
        runtime.search_run.state.run_dir(run_id)
        / "candidates"
        / task.candidate_id
        / "candidate.json"
    )
    legacy_candidate = load_json(candidate_path)
    legacy_candidate.pop("promotion_report")
    legacy_candidate.pop("promotion_evidence")
    write_json(candidate_path, legacy_candidate)
    before = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    process_score = before.score_report.aggregate_score  # type: ignore[union-attr]
    process_iterations = len(before.iterations)

    cached_report = runtime.run_verifier(
        run_id, task.candidate_id, scope="promotion"
    )
    assert cached_report.promotion_passed is True
    assert counter_path.read_text(encoding="utf-8") == "1"

    patch_path = runtime.promote(run_id, task.candidate_id)

    assert patch_path.exists()
    assert counter_path.read_text(encoding="utf-8") == "2"
    run = runtime.search_run.state.read_run(run_id)
    record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert run.state == "promoted"
    assert record.score_report is not None
    assert record.score_report.aggregate_score == process_score
    assert len(record.iterations) == process_iterations
    assert record.promotion_report is not None
    assert record.promotion_evidence is not None
    assert record.promotion_evidence.passed is True
    assert record.promotion_evidence.selected_git_head == run.selected_git_head
    assert record.promotion_evidence.git_head == run.selected_git_head
    assert record.promotion_evidence.artifact_hash == run.selected_artifact_hash
    process_log = record.score_report.verifier_results[0].log_path
    promotion_log = record.promotion_report.verifier_results[0].log_path
    assert process_log is not None and "logs/process" in process_log.as_posix()
    assert promotion_log is not None and "logs/promotion" in promotion_log.as_posix()


def test_promote_rejects_changed_selected_artifact_before_acceptance(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    counter_path = tmp_path / "promotion-count.txt"
    (project / "promotion.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        f"counter = Path({str(counter_path)!r})\n"
        "counter.write_text('ran')\n"
        "print(json.dumps({'accepted': True}))\n",
        encoding="utf-8",
    )
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["promotion_verifiers"] = [
        {
            "name": "full_acceptance",
            "role": "promotion_gate",
            "command": ["python", "promotion.py"],
            "timeout_seconds": 30,
        }
    ]
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py", project / "promotion.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    runtime.run_verifier(run_id, task.candidate_id)
    runtime.select(run_id)
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 999\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="selected artifact changed"):
        runtime.promote(run_id, task.candidate_id)

    assert not counter_path.exists()
    assert runtime.search_run.state.read_run(run_id).state == "ready_to_promote"
    record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert record.promotion_report is None
    assert record.promotion_evidence is None


def test_promote_rejects_concurrent_allowed_file_change_during_acceptance(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    started_path = tmp_path / "promotion-started"
    release_path = tmp_path / "promotion-release"
    (project / "promotion.py").write_text(
        "import json\n"
        "import time\n"
        "from pathlib import Path\n"
        f"started = Path({str(started_path)!r})\n"
        f"release = Path({str(release_path)!r})\n"
        "started.write_text('started')\n"
        "while not release.exists():\n"
        "    time.sleep(0.01)\n"
        "print(json.dumps({'accepted': True}))\n",
        encoding="utf-8",
    )
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["promotion_verifiers"] = [
        {
            "name": "full_acceptance",
            "role": "promotion_gate",
            "command": ["python", "promotion.py"],
            "timeout_seconds": 30,
        }
    ]
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py", project / "promotion.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    runtime.run_verifier(run_id, task.candidate_id)
    runtime.select(run_id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.promote, run_id, task.candidate_id)
        deadline = time.time() + 10
        while not started_path.exists() and time.time() < deadline:
            time.sleep(0.01)
        try:
            assert started_path.exists(), "promotion verifier did not start"
            task.workspace.joinpath("initial_program.py").write_text(
                "VALUE = 999\n", encoding="utf-8"
            )
        finally:
            release_path.write_text("release", encoding="utf-8")
        with pytest.raises(RuntimeError, match="fresh passing promotion evidence"):
            future.result(timeout=10)

    patch_path = runtime.search_run.state.run_dir(run_id) / "promotion" / f"{task.candidate_id}.patch"
    assert not patch_path.exists()
    assert runtime.search_run.state.read_run(run_id).state == "ready_to_promote"


@pytest.mark.parametrize("workspace_backend", ["copy", "git_worktree"])
def test_promote_patch_uses_selected_commit_after_evidence_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace_backend: str,
) -> None:
    project = make_project(tmp_path)
    (project / "promotion.py").write_text(
        "import json\nprint(json.dumps({'accepted': True}))\n",
        encoding="utf-8",
    )
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["workspace"] = {"backend": workspace_backend}
    spec_data["promotion_verifiers"] = [
        {
            "name": "full_acceptance",
            "role": "promotion_gate",
            "command": ["python", "promotion.py"],
            "timeout_seconds": 30,
        }
    ]
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py", project / "promotion.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    runtime.run_verifier(run_id, task.candidate_id)
    runtime.select(run_id)

    real_write_patch = runtime.promotion.io.write_patch

    def mutate_live_workspace_then_export(*args, **kwargs) -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            mutation = executor.submit(
                task.workspace.joinpath("initial_program.py").write_text,
                "VALUE = 999\n",
                encoding="utf-8",
            )
            mutation.result(timeout=5)
        real_write_patch(*args, **kwargs)

    monkeypatch.setattr(
        runtime.promotion.io, "write_patch", mutate_live_workspace_then_export
    )

    patch_path = runtime.promote(run_id, task.candidate_id)
    apply_target = tmp_path / "apply-target"
    copy_source_tree(project, apply_target)
    subprocess.run(["git", "apply", str(patch_path)], cwd=apply_target, check=True)

    assert (
        task.workspace.joinpath("initial_program.py").read_text(encoding="utf-8")
        == "VALUE = 999\n"
    )
    assert (
        apply_target.joinpath("initial_program.py").read_text(encoding="utf-8")
        == "VALUE = 1\n"
    )


def test_git_promotion_preserves_running_history_and_applies_one_squash_commit(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    source_head = git_commit_all(project, "source baseline")
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["workspace"] = {"backend": "git_worktree"}
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    assert task.workspace_creation_timing is not None
    assert task.workspace_creation_timing.outcome == "succeeded"
    assert task.workspace_creation_timing.duration_seconds >= 0

    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    git_commit_all(task.workspace, "autoresearch running commit")
    runtime.run_verifier(run_id, task.candidate_id)
    runtime.select(run_id)
    patch_path = runtime.promote(run_id, task.candidate_id)
    prepared = runtime.promotion_record(run_id)

    assert patch_path.is_file()
    assert prepared.strategy == "squash_commit"
    assert prepared.publication_artifact_ref is not None
    publication = prepared.publication_artifact_ref.id
    assert subprocess.check_output(
        ["git", "rev-list", "--count", f"{source_head}..{publication}"],
        cwd=task.workspace,
        text=True,
    ).strip() == "1"
    assert int(
        subprocess.check_output(
            ["git", "rev-list", "--count", f"{source_head}..HEAD"],
            cwd=task.workspace,
            text=True,
        ).strip()
    ) >= 2

    applied = runtime.apply_promotion(run_id)

    assert applied.state == "applied"
    assert applied.apply_timing is not None
    assert applied.apply_timing.outcome == "succeeded"
    assert applied.apply_timing.duration_seconds >= 0
    assert applied.applied_source_revision == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=project, text=True
    ).strip()
    assert subprocess.check_output(
        ["git", "rev-list", "--count", f"{source_head}..HEAD"],
        cwd=project,
        text=True,
    ).strip() == "1"
    assert project.joinpath("initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"
    assert not project.joinpath("results.tsv").exists()

    persisted = runtime.search_run.state.read_run(run_id)
    assert persisted.promotion is not None
    persisted.promotion = persisted.promotion.model_copy(
        update={
            "state": "prepared",
            "applied_source_revision": None,
            "apply_result": {},
            "applied_at": None,
        }
    )
    runtime.search_run.persist(persisted)
    recovered = runtime.apply_promotion(run_id)

    assert recovered.apply_result["method"] == "git_existing_cherry_pick"
    assert subprocess.check_output(
        ["git", "rev-list", "--count", f"{source_head}..HEAD"],
        cwd=project,
        text=True,
    ).strip() == "1"


def test_create_rejects_failed_workspace_capacity_preflight(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=4),
        [project / "evaluator.py"],
    )
    assert frozen.workspace_capacity_estimate is not None
    failed_estimate = frozen.workspace_capacity_estimate.model_copy(
        update={
            "available_bytes": 0,
            "fits_available_space": False,
        }
    )
    failed_frozen = frozen.model_copy(
        update={"workspace_capacity_estimate": failed_estimate}
    )
    (runtime.root_dir / "specs" / frozen.frozen_spec_id / "frozen_spec.json").write_text(
        json.dumps(failed_frozen.model_dump(mode="json")),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="capacity preflight failed"):
        runtime.create_run(frozen.frozen_spec_id)


def test_git_run_local_worktree_and_apply_work_inside_source_gp(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    project.joinpath(".gitignore").write_text(".gp/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    source_head = git_commit_all(project, "source baseline")
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["workspace"] = {"backend": "git_worktree"}
    runtime = FileSearchRuntime(project / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    assert task.workspace_managed_root is not None
    assert task.workspace_managed_root.is_relative_to(project / ".gp")
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    runtime.run_verifier(run_id, task.candidate_id)
    runtime.select(run_id)
    runtime.promote(run_id, task.candidate_id)
    runtime.apply_promotion(run_id)

    assert project.joinpath("initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"
    assert subprocess.check_output(
        ["git", "rev-list", "--count", f"{source_head}..HEAD"],
        cwd=project,
        text=True,
    ).strip() == "1"
    assert subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=project, text=True
    ).strip() == ""


def test_git_promotion_apply_is_noop_when_product_tree_is_unchanged(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    source_head = git_commit_all(project, "source baseline")
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["workspace"] = {"backend": "git_worktree"}
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    runtime.run_verifier(run_id, task.candidate_id)
    runtime.select(run_id)
    runtime.promote(run_id, task.candidate_id)
    applied = runtime.apply_promotion(run_id)

    assert applied.apply_result["method"] == "git_noop"
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=project, text=True
    ).strip() == source_head


def test_git_promotion_applies_nested_source_subdir_only(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    project = make_project(repository)
    repository.joinpath("README.md").write_text("root\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    source_head = git_commit_all(repository, "source baseline")
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["workspace"] = {"backend": "git_worktree"}
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    assert task.workspace_source_subdir == "project"
    assert task.workspace_managed_root is not None
    assert task.workspace == task.workspace_managed_root / "project"
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    runtime.run_verifier(run_id, task.candidate_id)
    runtime.select(run_id)
    runtime.promote(run_id, task.candidate_id)
    runtime.apply_promotion(run_id)

    assert project.joinpath("initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"
    assert repository.joinpath("README.md").read_text(encoding="utf-8") == "root\n"
    assert subprocess.check_output(
        ["git", "diff", "--name-only", f"{source_head}..HEAD"],
        cwd=repository,
        text=True,
    ).splitlines() == ["project/initial_program.py"]


def test_nested_git_workspace_rejects_committed_changes_outside_source_subdir(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    project = make_project(repository)
    repository.joinpath("README.md").write_text("root\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    git_commit_all(repository, "source baseline")
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["workspace"] = {"backend": "git_worktree"}
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    assert task.workspace_managed_root is not None

    task.workspace_managed_root.joinpath("README.md").write_text(
        "candidate escaped\n", encoding="utf-8"
    )
    git_commit_all(task.workspace_managed_root, "outside logical source")
    report = runtime.run_verifier(run_id, task.candidate_id)
    record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)

    assert report.process_passed is False
    assert "../README.md" in record.detected_changed_files
    assert record.changed_outside_allowed is True


def test_git_promotion_conflict_aborts_without_cherry_pick_state(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    git_commit_all(project, "source baseline")
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["workspace"] = {"backend": "git_worktree"}
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data), [project / "evaluator.py"]
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    runtime.run_verifier(run_id, task.candidate_id)
    runtime.select(run_id)
    runtime.promote(run_id, task.candidate_id)

    project.joinpath("initial_program.py").write_text(
        "VALUE = 99\n", encoding="utf-8"
    )
    source_revision = git_commit_all(project, "conflicting source change")

    with pytest.raises(RuntimeError, match="cherry-pick"):
        runtime.apply_promotion(run_id)

    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=project, text=True
    ).strip() == source_revision
    assert subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=project, text=True
    ).strip() == ""
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    ).returncode != 0
    failed_promotion = runtime.promotion_record(run_id)
    assert failed_promotion.state == "apply_failed"
    assert failed_promotion.apply_timing is not None
    assert failed_promotion.apply_timing.outcome == "failed"
    assert failed_promotion.apply_timing.duration_seconds >= 0


@pytest.mark.codex
def test_worker_policy_includes_host_capabilities_for_codex(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_host(project, "codex", strategy_name="random", max_parallel=1)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)

    plan = runtime.plan_next(run_id, requested_k=1)

    assert plan.worker_policy["host"] == "codex"
    assert plan.worker_policy["pool"]["launch_mode"] == "async"
    assert plan.worker_policy["pool"]["wait_mode"] == "wait_any"
    assert plan.worker_policy["pool"]["continuation_mode"] == "same_worker"
    assert plan.worker_policy["pool"]["wait_tool"] == "wait_agent"
    assert plan.worker_policy["pool"]["continue_tool"] == "followup_task"


@pytest.mark.pi
def test_worker_policy_uses_pi_rpc_native_session_resume(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "worker_budget": {
                "max_runtime_seconds": 600,
                "max_turns": 8,
                "on_exceed": "interrupt",
            },
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)

    plan = runtime.plan_next(run_id, requested_k=1)

    assert plan.worker_policy["host"] == "pi-rpc"
    assert plan.worker_policy["pool"]["launch_mode"] == "async"
    assert plan.worker_policy["pool"]["wait_mode"] == "wait_any"
    assert plan.worker_policy["pool"]["continuation_mode"] == "native_session"
    assert plan.worker_policy["pool"]["recovery_mode"] == "supervisor_persisted"
    assert plan.worker_policy["pool"]["wait_tool"] == "pi_search_pool_wait_any"


def test_start_agent_session_creates_context_handle_and_launch_payload(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "inner_agent": "autoresearch",
        },
        max_parallel=1,
    )
    spec_data = spec.model_dump(mode="json")
    spec_data["budget"]["max_parallel"] = 1
    frozen = runtime.freeze_spec(SearchSpec.model_validate(spec_data), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    session = runtime.start_agent_session(
        run_id, tasks[0].candidate_id, {"goal": "try one concrete variant"},
    )
    assert session.candidate_id == tasks[0].candidate_id
    assert session.workspace == tasks[0].workspace
    assert session.agent_session_id.startswith("agent_")
    assert session.launch["agent_type"] == "default"
    assert session.host == "codex"
    assert session.host_handle.host == "codex"
    assert session.agent_session_id in session.launch["message"]
    assert tasks[0].candidate_id in session.launch["message"]
    assert "required" not in session.launch

    repeated = runtime.start_agent_session(
        run_id, tasks[0].candidate_id, {"goal": "try one concrete variant"},
    )
    assert repeated == session
    assert runtime.candidate_sessions.state.read_agent_sessions(run_id) == [session]
    with pytest.raises(RuntimeError, match="different launch options"):
        runtime.start_agent_session(
            run_id, tasks[0].candidate_id, {"goal": "try a different variant"},
        )


def test_redispatch_candidate_preserves_frozen_legacy_native_role(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "inner_agent": "autoresearch",
            "worker_agent_type": "search_candidate_agent_deep",
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    first = runtime.start_agent_session(run_id, task.candidate_id, {"goal": "try flash"})

    redispatched = runtime.redispatch_candidate(
        run_id,
        task.candidate_id,
    )

    assert redispatched.agent_session_id != first.agent_session_id
    assert redispatched.candidate_id == first.candidate_id
    assert redispatched.workspace == first.workspace
    assert redispatched.launch["agent_type"] == "search_candidate_agent_deep"
    assert redispatched.agent_session_id in redispatched.launch["message"]
    assert "state_level_resume" in redispatched.launch["message"]

    refreshed_candidate = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    worker_policy = refreshed_candidate.task.strategy_metadata["worker_policy"]
    assert worker_policy["inner_agent"] == "autoresearch"
    assert worker_policy["legacy_worker_agent_type"] == (
        "search_candidate_agent_deep"
    )


def test_redispatch_context_includes_previous_progress_handoff(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = spec_with_host(
        project, "pi-rpc", strategy_name="random", max_parallel=1
    ).model_dump(mode="json")
    spec_data["strategy"]["worker_budget"] = {
        "max_runtime_seconds": 60,
        "max_turns": 8,
    }
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    first = runtime.start_agent_session(run_id, task.candidate_id)
    runtime.bind_agent_handle(
        first.agent_session_id,
        {
            "host": "pi-rpc",
            "external_id": first.agent_session_id,
            "metadata": {
                "timed_out": True,
                "assistant_text": None,
                "progress_handoff": {
                    "status": "timed_out",
                    "summary": "implemented parser skeleton",
                    "workspace": {"dirty": True, "changed_files": ["initial_program.py"]},
                    "verifier": {"count": 0},
                },
            },
        },
    )
    resumed = runtime.redispatch_candidate(
        run_id,
        task.candidate_id,
        worker_budget={"max_runtime_seconds": 120, "max_turns": 8},
    )

    context = runtime.get_agent_context(resumed.agent_session_id)

    assert context["resume"]["is_redispatch"] is True
    assert context["resume"]["latest_handoff"]["summary"] == "implemented parser skeleton"
    assert context["resume"]["previous_session_count"] == 1
    assert context["resume"]["latest_previous_session"] == {
        "agent_session_id": first.agent_session_id,
        "timed_out": True,
        "runner_failed": False,
        "assistant_summary": None,
        "progress_handoff": {
            "status": "timed_out",
            "summary": "implemented parser skeleton",
            "workspace": {"dirty": True, "changed_files": ["initial_program.py"]},
            "verifier": {"count": 0},
        },
        "error": None,
    }
    assert context["resume"]["workspace"]["dirty"] is False
    assert resumed.launch["budget_control"]["max_runtime_seconds"] == 120


@pytest.mark.codex
def test_start_agent_session_returns_codex_launch_payload(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_host(project, "codex", strategy_name="random", max_parallel=1)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    session = runtime.start_agent_session(run_id, task.candidate_id)

    assert "candidate_task" in session.launch["message"]
    assert "search_get_global_evidence" not in session.launch["message"]

    assert session.host == "codex"
    assert session.host_handle.host == "codex"
    assert session.host_handle.task_name == session.launch["task_name"]
    assert session.launch["tool"] == "spawn_agent"
    assert session.launch["agent_type"] == "default"
    assert session.launch["fork_turns"] == "none"
    assert "agent_session_id=" in session.launch["message"]


@pytest.mark.pi
def test_start_agent_session_returns_pi_rpc_launch_payload(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "worker_budget": {
                "max_runtime_seconds": 600,
                "max_turns": 8,
                "on_exceed": "interrupt",
            },
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    session = runtime.start_agent_session(run_id, task.candidate_id)

    assert session.host == "pi-rpc"
    assert session.host_handle.host == "pi-rpc"
    assert session.host_handle.external_id == session.agent_session_id
    assert session.launch["tool"] == "pi_rpc_worker"
    assert session.launch["run_id"] == run_id
    assert session.launch["root"] == str(runtime.root_dir)
    assert session.launch["cwd"] == str(task.workspace)
    assert task.workspace_access.native_cwd == task.workspace
    assert task.workspace_access.bootstrap_required is False
    assert task.workspace_access.confinement == "soft_path"
    assert session.launch["session_id"] == session.agent_session_id
    assert session.launch["budget_control"]["mode"] == "pi_rpc_process_watchdog"
    assert session.launch["budget_control"]["max_runtime_seconds"] == 600
    assert session.launch["budget_control"]["max_turns_hint"] == 8
    assert "search_get_agent_context" in session.launch["prompt"]
    assert str(task.workspace) not in session.launch["prompt"]


@pytest.mark.pi
def test_start_agent_session_accepts_one_dispatch_worker_budget(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "worker_budget": {
                "max_runtime_seconds": 600,
                "max_turns": 8,
                "on_exceed": "interrupt",
            },
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    long_session = runtime.start_agent_session(
        run_id,
        task.candidate_id,
        worker_budget={
            "max_runtime_seconds": 1800,
            "max_turns": 40,
            "on_exceed": "interrupt",
        },
    )
    default_session = runtime.redispatch_candidate(run_id, task.candidate_id)

    assert long_session.launch["budget_control"]["max_runtime_seconds"] == 1800
    assert "assigned_worker_budget={'max_runtime_seconds': 1800" in long_session.launch[
        "prompt"
    ]
    assert default_session.launch["budget_control"]["max_runtime_seconds"] == 600
    reloaded = runtime.search_run.state.read_frozen_spec(frozen.frozen_spec_id)
    assert reloaded.spec.strategy.worker_budget is not None
    assert reloaded.spec.strategy.worker_budget.max_runtime_seconds == 600


@pytest.mark.codex
def test_launch_keeps_candidate_proposal_when_main_directive_is_shared(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_host(
        project,
        "codex",
        strategy_name="agent_guided",
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(
        run_id,
        plan.plan_id,
        [
            CandidateProposal(
                intent="explore scratch-resident batching",
                hypothesis="reuse scratch slots across dependent operations",
                instructions=["measure one complete batch before interleaving"],
            )
        ],
    )[0]

    session = runtime.start_agent_session(
        run_id,
        task.candidate_id,
        {"goal": "perform deep optimization"},
    )

    assert "candidate_intent: explore scratch-resident batching" in session.launch[
        "message"
    ]
    assert "candidate_hypothesis: reuse scratch slots" in session.launch["message"]
    assert "main_directive.goal: perform deep optimization" in session.launch["message"]


@pytest.mark.codex
def test_redispatch_candidate_overrides_codex_worker_budget(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_host(project, "codex", strategy_name="random", max_parallel=1)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    first = runtime.start_agent_session(run_id, task.candidate_id)

    redispatched = runtime.redispatch_candidate(
        run_id,
        task.candidate_id,
        worker_budget={"max_runtime_seconds": 30, "max_turns": 12, "on_exceed": "interrupt"},
    )

    assert redispatched.agent_session_id != first.agent_session_id
    assert redispatched.launch["agent_type"] == "default"
    assert redispatched.launch["budget_control"] == {
        "mode": "parent_watchdog",
        "max_runtime_seconds": 30,
        "initial_wait_timeout_ms": 20000,
        "soft_closeout_seconds": 10,
        "closeout_tool": "send_message",
        "closeout_target": redispatched.launch["task_name"],
        "closeout_message": (
            "Worker 的截止时间临近。停止启动新工作；如有需要，最后运行一次 "
            "search_run_verifier，更新 .tmp/handoff.json，并返回简洁摘要。"
        ),
        "final_wait_timeout_ms": 10000,
        "on_exceed": "interrupt",
        "interrupt_tool": "interrupt_agent",
        "interrupt_target": redispatched.launch["task_name"],
        "max_turns_hint": 12,
    }


@pytest.mark.codex
def test_codex_worker_budget_flows_to_watchdog_launch_payload(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "codex",
            "worker_budget": {
                "max_runtime_seconds": 600,
                "max_turns": 8,
                "on_exceed": "interrupt",
            },
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    session = runtime.start_agent_session(run_id, task.candidate_id)

    assert plan.worker_policy["worker_budget"] == {
        "max_runtime_seconds": 600,
        "max_turns": 8,
        "on_exceed": "interrupt",
        "min_runtime_seconds": None,
        "min_verifier_runs": None,
    }
    assert session.launch["budget_control"] == {
        "mode": "parent_watchdog",
        "max_runtime_seconds": 600,
        "initial_wait_timeout_ms": 555000,
        "soft_closeout_seconds": 45,
        "closeout_tool": "send_message",
        "closeout_target": session.launch["task_name"],
        "closeout_message": (
            "Worker 的截止时间临近。停止启动新工作；如有需要，最后运行一次 "
            "search_run_verifier，更新 .tmp/handoff.json，并返回简洁摘要。"
        ),
        "final_wait_timeout_ms": 45000,
        "on_exceed": "interrupt",
        "interrupt_tool": "interrupt_agent",
        "interrupt_target": session.launch["task_name"],
        "max_turns_hint": 8,
    }


@pytest.mark.pi
def test_pi_worker_budget_accepts_autoresearch_lease_fields(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "worker_budget": {
                "min_runtime_seconds": 300,
                "min_verifier_runs": 1,
                "max_runtime_seconds": 420,
                "on_exceed": "interrupt",
            },
        },
        max_parallel=1,
    )

    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)

    assert frozen.spec.strategy.worker_budget is not None
    assert frozen.spec.strategy.worker_budget.min_runtime_seconds == 300
    assert frozen.spec.strategy.worker_budget.min_verifier_runs == 1
    assert session.launch["budget_control"]["autoresearch_lease"][
        "min_runtime_seconds"
    ] == 300


@pytest.mark.pi
def test_freeze_rejects_worker_lease_fields_in_strategy_config(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "worker_budget": {
                "max_runtime_seconds": 420,
                "on_exceed": "interrupt",
            },
            "config": {
                "min_runtime_seconds": 300,
                "min_verifier_runs": 1,
            },
        },
        max_parallel=1,
    )

    with pytest.raises(ValueError, match="strategy.worker_budget"):
        runtime.freeze_spec(spec, [project / "evaluator.py"])


def test_freeze_rejects_unknown_global_evidence_mode(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_for(project).model_copy(deep=True)
    spec.strategy.config["global_evidence_mode"] = "sometimes"

    with pytest.raises(ValueError, match="global_evidence_mode"):
        runtime.freeze_spec(spec, [project / "evaluator.py"])


def test_freeze_persists_global_evidence_mode_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_for(project)
    monkeypatch.setenv("GOAL_PLUS_GLOBAL_EVIDENCE_MODE", "independent")

    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])

    assert "global_evidence_mode" not in spec.strategy.config
    assert frozen.spec.strategy.config["global_evidence_mode"] == "independent"
    persisted = load_json(
        runtime.specs_dir / frozen.frozen_spec_id / "frozen_spec.json"
    )
    assert persisted["spec"]["strategy"]["config"][
        "global_evidence_mode"
    ] == "independent"


def test_freeze_rejects_conflicting_global_evidence_mode_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_for(project).model_copy(deep=True)
    spec.strategy.config["global_evidence_mode"] = "auto"
    monkeypatch.setenv("GOAL_PLUS_GLOBAL_EVIDENCE_MODE", "independent")

    with pytest.raises(ValueError, match="conflicts"):
        runtime.freeze_spec(spec, [project / "evaluator.py"])


@pytest.mark.codex
def test_codex_worker_launch_options_flow_to_spawn_payload(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "codex",
            "worker_launch": {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
                "service_tier": "priority",
            },
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    session = runtime.start_agent_session(run_id, task.candidate_id)

    assert plan.worker_policy["worker_launch"] == {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "service_tier": "priority",
    }
    assert session.launch["model"] == "gpt-5.6-terra"
    assert session.launch["reasoning_effort"] == "high"
    assert session.launch["service_tier"] == "priority"


@pytest.mark.pi
def test_pi_rpc_rejects_unsupported_worker_service_tier(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "worker_budget": {"max_runtime_seconds": 60},
            "worker_launch": {"service_tier": "priority"},
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)

    with pytest.raises(ValueError, match="service_tier"):
        runtime.plan_next(run_id, requested_k=1)


@pytest.mark.pi
def test_pi_rpc_rejects_unsupported_selected_model_service_tier(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    runtime.models.list_available_models = lambda host, query=None: {  # type: ignore[method-assign]
        "host": host,
        "adapter_version": "pi-rpc-v1",
        "models": [
            {
                "model": "test/pi-model-v1",
                "model_id": "pi-model-v1",
                "provider": "test",
                "display_name": "Pi Model V1",
            }
        ],
    }
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "worker_budget": {"max_runtime_seconds": 60},
            "models": [
                {
                    "model": "pi-model-v1",
                    "count": 1,
                    "service_tier": "priority",
                }
            ],
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)

    with pytest.raises(ValueError, match="service_tier"):
        runtime.plan_next(run_id, requested_k=1)


def test_host_worker_budget_rejects_unenforceable_limits(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")

    codex_spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "codex",
            "worker_budget": {"max_turns": 8},
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(codex_spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    with pytest.raises(ValueError, match="codex worker_budget requires max_runtime_seconds"):
        runtime.plan_next(run_id, requested_k=1)

    pi_runtime = FileSearchRuntime(tmp_path / ".search-pi")
    pi_spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "worker_budget": {"max_turns": 8},
        },
        max_parallel=1,
    )
    frozen = pi_runtime.freeze_spec(pi_spec, [project / "evaluator.py"])
    run_id = pi_runtime.create_run(frozen.frozen_spec_id)
    with pytest.raises(ValueError, match="pi-rpc worker_budget requires max_runtime_seconds"):
        pi_runtime.plan_next(run_id, requested_k=1)


@pytest.mark.codex
def test_bind_agent_handle_records_codex_task_name(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_host(project, "codex", strategy_name="random", max_parallel=1)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)

    updated = runtime.bind_agent_handle(
        session.agent_session_id,
        {"host": "codex", "task_name": "search_agent_0001", "nickname": "search worker"},
    )

    assert updated.host == "codex"
    assert updated.host_handle.task_name == "search_agent_0001"
    assert updated.host_handle.nickname == "search worker"


@pytest.mark.codex
def test_bind_agent_handle_harvests_workspace_handoff_into_history(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_host(project, "codex", strategy_name="random", max_parallel=1)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    handoff_path = task.workspace / ".tmp" / "handoff.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        handoff_path,
        {
            "summary": "tested a distinct allocation strategy",
            "key_results": ["iteration 2 improved the score"],
            "pitfalls": [],
            "blockers": [],
            "next_steps": ["test the portable subset"],
            "verifier_assessment": {
                "status": "adequate",
                "evidence": ["deterministic score"],
                "impact": "safe to continue",
                "recommended_action": "keep_spec",
            },
        },
    )

    updated = runtime.bind_agent_handle(
        session.agent_session_id,
        {"host": "codex", "task_name": "search_agent_0001"},
    )

    progress = updated.host_handle.metadata["progress_handoff"]
    assert progress["source_path"] == ".tmp/handoff.json"
    assert progress["model_handoff"]["summary"] == "tested a distinct allocation strategy"
    history = runtime.list_history(run_id)
    assert history["candidates"][0]["summary"] == "tested a distinct allocation strategy"
    report = runtime.report(run_id).read_text(encoding="utf-8")
    assert "tested a distinct allocation strategy" in report

    handoff_path.write_text("{not-json", encoding="utf-8")
    rebound = runtime.bind_agent_handle(
        session.agent_session_id,
        {"host": "codex", "task_name": "search_agent_0001"},
    )
    assert "JSONDecodeError" in rebound.host_handle.metadata["progress_handoff_error"]
    assert (
        rebound.host_handle.metadata["progress_handoff"]["model_handoff"]["summary"]
        == "tested a distinct allocation strategy"
    )


@pytest.mark.codex
def test_codex_continue_agent_session_uses_bound_worker_and_budget(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_host(project, "codex", strategy_name="random", max_parallel=1)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    runtime.bind_agent_handle(
        session.agent_session_id,
        {"host": "codex", "task_name": "search_agent_0001"},
    )

    continued = runtime.continue_agent_session(
        session.agent_session_id,
        worker_budget={"max_runtime_seconds": 900, "on_exceed": "interrupt"},
    )

    assert continued.launch["tool"] == "followup_task"
    assert continued.launch["target"] == "search_agent_0001"
    assert continued.launch["budget_control"]["max_runtime_seconds"] == 900
    assert continued.counters["resume_dispatches"] == 1
    assert "沿用已加载的运行时上下文和 Evidence" in continued.launch["message"]
    assert "返回前，在候选工作区创建 `.tmp/handoff.json`" not in continued.launch[
        "message"
    ]


@pytest.mark.pi
def test_pi_rpc_continue_agent_session_reuses_native_session(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "worker_budget": {
                "max_runtime_seconds": 600,
                "on_exceed": "interrupt",
            },
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    runtime.bind_agent_handle(
        session.agent_session_id,
        {
            "host": "pi-rpc",
            "external_id": session.agent_session_id,
            "metadata": {
                "event_log": "/tmp/pi-rpc-agent_0001.jsonl",
                "pi_metrics": {
                    "final_last_entry_id": "entry_3",
                    "final_entry_count": 3,
                    "usage_total": {"input": 25},
                    "duration_seconds": 1.5,
                    "started_at": "2026-07-19T00:00:00Z",
                },
            },
        },
    )

    continued = runtime.continue_agent_session(session.agent_session_id)

    assert continued.agent_session_id == session.agent_session_id
    assert continued.launch["session_id"] == session.agent_session_id
    assert continued.launch["continuation"] == "native_session"
    assert continued.counters["resume_dispatches"] == 1
    assert continued.launch["metrics_baseline"]["last_entry_id"] == "entry_3"
    assert "continue_existing_agent_session=true" in continued.launch["prompt"]
    context = runtime.get_agent_context(session.agent_session_id)
    assert context["resume"]["is_redispatch"] is False
    assert context["resume"]["is_native_session_resume"] is True
    assert context["resume"]["mode"] == "native_session"
    assert context["resume"]["dispatch_count"] == 1

    report = runtime.report(run_id).read_text(encoding="utf-8")
    assert "| Session | Host | Candidate | Verifier Runs |" in report
    assert "| Session | Host | Handle | Candidate | Verifier Runs |" not in report


def test_plan_next_caps_batch_size_to_max_parallel(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = spec_for(project, max_parallel=4).model_dump(mode="json")
    spec_data["budget"]["max_parallel"] = 2
    frozen = runtime.freeze_spec(SearchSpec.model_validate(spec_data), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=4)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    assert plan.requested_k == 4
    assert plan.planned_k == 2
    assert [task.candidate_id for task in tasks] == ["c001", "c002"]

    with pytest.raises(RuntimeError, match="one initial SearchPlan"):
        runtime.plan_next(run_id, requested_k=4)


def test_rolling_candidates_remains_scheduler_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    spec_data = spec_for(project).model_dump(mode="json")
    spec_data["strategy"]["orchestration_mode"] = "rolling_candidates"
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    monkeypatch.setattr(
        scheduler_module,
        "kick_scheduler",
        lambda *args, **kwargs: pytest.fail("rolling_candidates entered Search Scheduler"),
    )

    assert runtime.search_scheduler_enabled(run_id) is False
    wake_scheduler_after_annotation(runtime.root_dir, run_id)


def test_parallel_loops_rejects_second_plan_and_reuses_initial_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = spec_for(project, max_parallel=4).model_dump(mode="json")
    spec_data["budget"]["max_parallel"] = 2
    spec_data["strategy"].update(
        {
            "name": "random",
            "worker_host": "codex",
            "orchestration_mode": "parallel_loops",
        }
    )
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)

    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    assert [task.candidate_id for task in tasks] == ["c001", "c002"]
    with pytest.raises(RuntimeError, match="one initial SearchPlan"):
        runtime.plan_next(run_id, requested_k=1)

    def reject_scheduler_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("default Search entered Search Scheduler execution")

    for owner, method_name in (
        (runtime.candidate_sessions.admission, "assert_candidate_execution_allowed"),
        (runtime._adaptive_search, "resolve_scheduler_barrier"),
        (runtime.settlement.lanes, "ensure_tasks_locked"),
    ):
        monkeypatch.setattr(
            owner,
            method_name,
            reject_scheduler_call,
        )

    assert runtime.search_scheduler_enabled(run_id) is False
    monkeypatch.setattr(scheduler_module, "kick_scheduler", reject_scheduler_call)
    wake_scheduler_after_annotation(runtime.root_dir, run_id)

    with pytest.raises(RuntimeError, match="requires.*adaptive_search"):
        runtime.list_allocation_decisions(run_id)
    with pytest.raises(RuntimeError, match="requires.*adaptive_search"):
        runtime.apply_allocation_decision(run_id, "allocation_0001")

    session = runtime.start_agent_session(run_id, tasks[0].candidate_id)
    assert "expansion_action_context" not in runtime.get_agent_context(
        session.agent_session_id
    )
    report = runtime.run_verifier(
        run_id,
        tasks[0].candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="verify the ordinary parallel loop path",
    )
    record = runtime.candidate_creation.state.read_candidate(run_id, tasks[0].candidate_id)

    assert report.allocation_decision is None
    assert record.iterations[-1].schedule_task_id is None
    assert record.iterations[-1].allocation_decision_id is None

    snapshot = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    assert "search_scheduler" not in snapshot["run"]
    assert "allocation_decisions" not in snapshot["run"]
    assert "current_value_estimate" not in snapshot["candidates"][tasks[0].candidate_id]

    report_data = build_html_report_data(runtime.root_dir, run_id)
    [task_report] = report_data["search_tasks"]
    assert "allocation_decisions" not in task_report
    assert "search_graph" not in task_report
    assert "current_value_estimate" not in task_report["candidates"][0]
    assert "Reward / allocation" not in render_html_report(report_data)

    continued = runtime.continue_agent_session(session.agent_session_id)
    assert continued.agent_session_id == session.agent_session_id
    assert continued.candidate_id == tasks[0].candidate_id


def test_start_agent_session_allocates_unique_ids_under_parallel_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = spec_for(project, max_parallel=2).model_dump(mode="json")
    spec_data["budget"]["max_parallel"] = 2
    frozen = runtime.freeze_spec(SearchSpec.model_validate(spec_data), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    original_load_run = runtime.candidate_sessions.state.read_run
    loaded_count = 0
    loaded_lock = threading.Lock()
    second_loaded = threading.Event()

    def load_run_with_overlap(load_run_id: str):
        nonlocal loaded_count
        run = original_load_run(load_run_id)
        if load_run_id == run_id:
            with loaded_lock:
                loaded_count += 1
                current_count = loaded_count
                if loaded_count == 2:
                    second_loaded.set()
            if current_count == 1:
                second_loaded.wait(timeout=0.25)
        return run

    monkeypatch.setattr(runtime.candidate_sessions.state, "read_run", load_run_with_overlap)
    start_barrier = threading.Barrier(2)

    def start(candidate_id: str):
        start_barrier.wait(timeout=5)
        return runtime.start_agent_session(run_id, candidate_id, {"goal": candidate_id})

    with ThreadPoolExecutor(max_workers=2) as pool:
        sessions = list(pool.map(start, [task.candidate_id for task in tasks]))

    assert sorted(session.agent_session_id for session in sessions) == [
        runtime.candidate_sessions._make_agent_session_id(run_id, 1),
        runtime.candidate_sessions._make_agent_session_id(run_id, 2),
    ]
    assert sorted(session.candidate_id for session in sessions) == ["c001", "c002"]
    assert sorted(session.agent_session_id for session in runtime.candidate_sessions.state.read_agent_sessions(run_id)) == [
        runtime.candidate_sessions._make_agent_session_id(run_id, 1),
        runtime.candidate_sessions._make_agent_session_id(run_id, 2),
    ]
    assert original_load_run(run_id).next_agent_session_index == 3


def test_get_agent_context_has_only_authoritative_worker_fields(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
        },
        max_parallel=2,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    session = runtime.start_agent_session(run_id, tasks[0].candidate_id, {"goal": "iterate"})

    context = runtime.get_agent_context(session.agent_session_id)
    for forbidden in (
        "status",
        "phase",
        "visibility_mode",
        "budget",
        "peer_status",
        "observations",
        "selected_model",
        "model_provenance",
        "host",
        "host_handle",
        "directive",
        "workspace",
        "workspace_access",
        "orchestration_mode",
        "search_scheduler_enabled",
        "run_budget",
    ):
        assert forbidden not in context, f"get_agent_context must not return {forbidden}"
    assert context["candidate_id"] == tasks[0].candidate_id
    assert set(context["candidate_task"]) == {
        "hypothesis",
        "workspace",
        "allowed_files",
        "denied_files",
        "instructions",
    }
    assert context["candidate_task"]["workspace"] == str(tasks[0].workspace)
    assert "expected_artifacts" not in context["candidate_task"]
    assert "proposal" not in context["candidate_task"]
    assert "strategy_metadata" not in context["candidate_task"]
    assert "expansion_action_context" not in context
    assert "history" not in context
    assert "iterations" not in context
    assert "results" not in context
    assert context["iteration_count"] == 0
    assert context["recent_iterations"] == []


def test_agent_session_ids_are_unique_across_runs(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=1), [project / "evaluator.py"])

    first_run_id = runtime.create_run(frozen.frozen_spec_id)
    second_run_id = runtime.create_run(frozen.frozen_spec_id)
    first_plan = runtime.plan_next(first_run_id, requested_k=1)
    first_task = runtime.start_batch(first_run_id, first_plan.plan_id)[0]
    second_plan = runtime.plan_next(second_run_id, requested_k=1)
    second_task = runtime.start_batch(second_run_id, second_plan.plan_id)[0]

    first = runtime.start_agent_session(first_run_id, first_task.candidate_id)
    second = runtime.start_agent_session(second_run_id, second_task.candidate_id)

    assert first.agent_session_id != second.agent_session_id
    assert first_run_id.removeprefix("run_") in first.agent_session_id
    assert second_run_id.removeprefix("run_") in second.agent_session_id
    assert runtime.get_agent_context(first.agent_session_id)["run_id"] == first_run_id
    assert runtime.get_agent_context(second.agent_session_id)["run_id"] == second_run_id


def test_legacy_agent_session_id_collision_is_not_silent(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=1), [project / "evaluator.py"])

    first_run_id = runtime.create_run(frozen.frozen_spec_id)
    second_run_id = runtime.create_run(frozen.frozen_spec_id)
    first_plan = runtime.plan_next(first_run_id, requested_k=1)
    second_plan = runtime.plan_next(second_run_id, requested_k=1)
    first_task = runtime.start_batch(first_run_id, first_plan.plan_id)[0]
    second_task = runtime.start_batch(second_run_id, second_plan.plan_id)[0]
    first = runtime.start_agent_session(first_run_id, first_task.candidate_id)
    second = runtime.start_agent_session(second_run_id, second_task.candidate_id)

    legacy_first = first.model_copy(update={"agent_session_id": "agent_001"})
    legacy_second = second.model_copy(update={"agent_session_id": "agent_001"})
    runtime.candidate_sessions.state.write_agent_session(legacy_first)
    runtime.candidate_sessions.state.write_agent_session(legacy_second)

    with pytest.raises(RuntimeError, match="ambiguous agent_session_id"):
        runtime.get_agent_context("agent_001")


def test_agent_guided_strategy_requires_and_validates_proposals(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {"name": "agent_guided"},
        max_parallel=3,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)

    plan1 = runtime.plan_next(run_id, requested_k=1)
    assert plan1.requires_agent_proposals is True
    with pytest.raises(ValueError):
        runtime.start_batch(run_id, plan1.plan_id)

    first_tasks = runtime.start_batch(
        run_id,
        plan1.plan_id,
        [CandidateProposal(intent="bootstrap from source")],
    )
    assert first_tasks[0].base_candidate_id is None
    with pytest.raises(RuntimeError, match="one initial SearchPlan"):
        runtime.plan_next(run_id, requested_k=1)


def test_git_worktree_start_batch_recovers_after_materialization_before_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = spec_with_strategy(
        project,
        {"name": "random"},
        max_parallel=1,
    ).model_dump(mode="json")
    spec_data["workspace"] = {"backend": "git_worktree"}
    spec = SearchSpec.model_validate(spec_data)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, 1)
    original_write = runtime.candidate_creation.state.write_candidate
    failed = False

    def fail_once(run_id_arg: str, record: CandidateRecord) -> None:
        nonlocal failed
        if not failed:
            failed = True
            write_json(
                runtime.candidate_creation.state.candidate_dir(run_id_arg, record.candidate_id)
                / "candidate.json",
                record.model_dump(mode="json"),
            )
            raise RuntimeError("simulated state write failure")
        original_write(run_id_arg, record)

    monkeypatch.setattr(runtime.candidate_creation.state, "write_candidate", fail_once)

    with pytest.raises(RuntimeError, match="simulated state write failure"):
        runtime.start_batch(run_id, plan.plan_id)

    tasks = runtime.start_batch(run_id, plan.plan_id)
    assert [task.candidate_id for task in tasks] == ["c001"]
    assert runtime.status(run_id).candidates_total == 1
    assert (
        runtime.candidate_creation.state.candidate_dir(run_id, "c001") / "task.json"
    ).is_file()


def test_start_batch_is_serialized_and_idempotent_for_same_plan(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = spec_with_strategy(
        project,
        {"name": "random"},
        max_parallel=2,
    ).model_dump(mode="json")
    spec_data["workspace"] = {"backend": "git_worktree"}
    spec = SearchSpec.model_validate(spec_data)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, 2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(runtime.start_batch, run_id, plan.plan_id)
            for _ in range(2)
        ]
        batches = [future.result() for future in futures]

    assert [[task.candidate_id for task in batch] for batch in batches] == [
        ["c001", "c002"],
        ["c001", "c002"],
    ]
    assert runtime.status(run_id).candidates_total == 2


def test_random_strategy_gen1_independent_bootstrap(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(project, {"name": "random"}, max_parallel=4)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)

    plan = runtime.plan_next(run_id, 2)

    assert plan.requires_agent_proposals is False
    assert plan.strategy_trace["selection_rule"] == "独立源码分支"
    assert "parent_candidate_id" not in plan.strategy_trace
    assert len(plan.work_orders) == 2
    assert all(wo.metadata["strategy"] == "parallel_loops" for wo in plan.work_orders)

    tasks = runtime.start_batch(run_id, plan.plan_id)
    assert all(t.base_candidate_id is None for t in tasks)


def test_random_strategy_name_normalizes_case_and_dash(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")

    for name in ("Random", "random-mode", "RANDOM_MODE"):
        spec = spec_with_strategy(project, {"name": name}, max_parallel=4)
        frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
        run_id = runtime.create_run(frozen.frozen_spec_id)

        plan = runtime.plan_next(run_id, 2)

        assert plan.strategy_trace["selection_rule"] == "独立源码分支"
        assert plan.requires_agent_proposals is False


@pytest.mark.parametrize(
    ("host", "strategy_name", "requires_proposals", "expected_launch"),
    [
        (
            "codex",
            "default",
            True,
            {"tool": "spawn_agent", "agent_type": "default"},
        ),
        (
            "codex",
            "random-mode",
            False,
            {"tool": "spawn_agent", "agent_type": "default"},
        ),
    ],
)
@pytest.mark.codex
def test_codex_creates_sessions_for_portable_strategy_modes(
    tmp_path: Path,
    host: str,
    expected_launch: dict[str, object],
    strategy_name: str,
    requires_proposals: bool,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_host(project, host, strategy_name=strategy_name, max_parallel=1)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)

    plan = runtime.plan_next(run_id, requested_k=1)

    assert plan.worker_policy["host"] == host
    assert plan.requires_agent_proposals is requires_proposals
    if requires_proposals:
        tasks = runtime.start_batch(
            run_id,
            plan.plan_id,
            [CandidateProposal(intent=f"{host} {strategy_name} candidate")],
        )
    else:
        tasks = runtime.start_batch(run_id, plan.plan_id)

    session = runtime.start_agent_session(run_id, tasks[0].candidate_id)

    assert session.host == host
    assert session.host_handle.host == host
    assert session.agent_session_id in (
        session.launch.get("prompt") or session.launch.get("message")
    )
    for key, value in expected_launch.items():
        assert session.launch[key] == value


@pytest.mark.parametrize(
    "strategy_name",
    ["independent_branches", "openevolve"],
)
@pytest.mark.codex
def test_codex_rejects_non_portable_strategies(
    tmp_path: Path,
    strategy_name: str,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_host(
        project,
        "codex",
        strategy_name=strategy_name,
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)

    with pytest.raises(ValueError, match=f"codex.*{strategy_name}"):
        runtime.plan_next(run_id, requested_k=1)


def test_removed_strategy_driver_fields_are_rejected(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["strategy"].update(
        {
            "driver": "python",
            "ref": "some.module:Strategy",
        }
    )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SearchSpec.model_validate(data)


def test_run_verifier_records_edit_surface_violation_in_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    candidate_id = tasks[0].candidate_id
    session = runtime.start_agent_session(run_id, candidate_id, {"goal": "cheat"})

    # Worker touches a denied file.
    (tasks[0].workspace / "config.yaml").write_text("name: tampered\n", encoding="utf-8")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"combined_score": 0.9, "valid": true}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)
    runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="Probe a denied edit",
    )

    record = runtime.candidate_creation.state.read_candidate(run_id, candidate_id)
    it = record.iterations[-1]
    assert it.touched_denied_files is True
    assert "config.yaml" in it.changed_files


def test_run_verifier_reports_and_cleans_verifier_workspace_side_effect(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "if os.environ['GOAL_PLUS_VERIFIER_PHASE'] == 'candidate':\n"
        "    output = Path('.goal-plus-verifiers/generated.bin')\n"
        "    output.parent.mkdir(parents=True, exist_ok=True)\n"
        "    output.write_text('compiled', encoding='utf-8')\n"
        "print(json.dumps({'combined_score': 1.0}))\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, workspace = create_candidate(runtime, project)
    (workspace / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")

    report = runtime.run_verifier(run_id, candidate_id)

    result = report.verifier_results[0]
    assert report.process_passed is False
    assert result.failure_class == "VerifierWorkspaceSideEffect"
    assert result.metrics["verifier_workspace_side_effects"] == [
        ".goal-plus-verifiers/generated.bin"
    ]
    assert result.metrics["cleanup_failures"] == []
    assert result.metrics["infrastructure_failure"] is True
    assert result.metrics["candidate_action"] == "stop_and_report"
    assert not (workspace / ".goal-plus-verifiers/generated.bin").exists()
    assert report.disposition == "failure"
    assert (workspace / "initial_program.py").read_text(encoding="utf-8") == "VALUE = 0\n"
    attempt = runtime.list_iterations(run_id, candidate_id)[0]["git_head"]
    assert subprocess.check_output(
        ["git", "show", f"{attempt}:initial_program.py"],
        cwd=workspace,
        text=True,
    ) == "VALUE = 1\n"


def test_run_verifier_classifies_legacy_generated_verifier_file_as_infrastructure(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, workspace = create_candidate(runtime, project)
    generated = workspace / ".goal-plus-verifiers/generated.bin"
    generated.parent.mkdir(parents=True)
    generated.write_text("legacy side effect", encoding="utf-8")

    report = runtime.run_verifier(run_id, candidate_id)

    result = report.verifier_results[0]
    assert report.process_passed is False
    assert result.failure_class == "VerifierWorkspaceSideEffect"
    assert result.metrics["infrastructure_failure"] is True
    assert result.metrics["candidate_action"] == "stop_and_report"
    assert report.disposition == "failure"
    assert runtime.search_bootstrap.repository.git_status(workspace) == []
    attempt = runtime.list_iterations(run_id, candidate_id)[0]["git_head"]
    assert subprocess.check_output(
        ["git", "show", f"{attempt}:.goal-plus-verifiers/generated.bin"],
        cwd=workspace,
        text=True,
    ) == "legacy side effect"


def test_run_verifier_records_failure_class_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    candidate_id = tasks[0].candidate_id
    session = runtime.start_agent_session(run_id, candidate_id, {"goal": "iterate"})

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)
    report = runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="Measure timeout handling",
    )

    assert report.aggregate_score == 0.0
    assert report.attempt_id is not None
    assert report.attempt_iteration == 1
    assert report.attempt_artifact_ref is not None
    record = runtime.candidate_creation.state.read_candidate(run_id, candidate_id)
    assert len(record.iterations) == 1
    it = record.iterations[-1]
    assert it.failure_class == "Timeout"
    assert it.score == 0.0
    assert it.metrics["_candidate_attempt"]["id"] == report.attempt_id
    assert it.artifact_ref == report.attempt_artifact_ref


def test_git_worktree_verifier_start_failure_records_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    _initialize_source_repository(project)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _spec_with_workspace_backend(project, "git_worktree"),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    def fail_to_start(*args, **kwargs):
        raise OSError("verifier executable unavailable")

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fail_to_start)

    report = runtime.run_verifier(run_id, task.candidate_id)

    assert report.process_passed is False
    assert report.aggregate_score == 0.0
    assert report.attempt_id is not None
    assert report.attempt_iteration == 1
    assert report.attempt_artifact_ref is not None
    assert report.verifier_results[0].failure_class == "VerifierStartFailed"
    record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert len(record.iterations) == 1
    iteration = record.iterations[0]
    assert iteration.failure_class == "VerifierStartFailed"
    assert iteration.metrics["_candidate_attempt"]["id"] == report.attempt_id
    assert iteration.artifact_ref == report.attempt_artifact_ref


def test_git_worktree_verifier_command_failure_records_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    _initialize_source_repository(project)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _spec_with_workspace_backend(project, "git_worktree"),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    def fail_verifier(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=2,
            stdout="",
            stderr="candidate verifier failed",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fail_verifier)

    report = runtime.run_verifier(run_id, task.candidate_id)

    assert report.process_passed is False
    assert report.aggregate_score == 0.0
    assert report.attempt_id is not None
    assert report.attempt_iteration == 1
    assert report.attempt_artifact_ref is not None
    assert report.verifier_results[0].failure_class == "VerifierCommandFailed"
    record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert len(record.iterations) == 1
    iteration = record.iterations[0]
    assert iteration.failure_class == "VerifierCommandFailed"
    assert iteration.metrics["_candidate_attempt"]["id"] == report.attempt_id
    assert iteration.artifact_ref == report.attempt_artifact_ref


def test_process_verifier_requires_time_for_suite_and_closeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "config": {"reserve_closeout_seconds": 20},
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    candidate = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, candidate.candidate_id)
    monkeypatch.setenv("GOAL_PLUS_OUTER_DEADLINE_AT", str(time.time() + 40))

    with pytest.raises(RuntimeError, match="VerifierDeadlineInsufficient"):
        runtime.run_verifier(
            run_id,
            candidate.candidate_id,
            agent_session_id=session.agent_session_id,
            hypothesis="Do not start a verifier that can consume closeout",
        )

    assert runtime.list_iterations(run_id, candidate.candidate_id) == []
    assert runtime.search_run.state.read_run(run_id).state == RunState.WAITING_FOR_WORKERS


def test_list_iterations_empty_for_fresh_candidate(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    iterations = runtime.list_iterations(run_id, tasks[0].candidate_id)
    assert iterations == []


def test_run_verifier_records_iteration_with_agent_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "inner_agent": "autoresearch",
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    candidate_id = tasks[0].candidate_id
    session = runtime.start_agent_session(run_id, candidate_id, {"goal": "iterate"})

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"combined_score": 0.7, "valid": true}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)
    runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="Record session provenance",
    )

    record = runtime.candidate_creation.state.read_candidate(run_id, candidate_id)
    it = record.iterations[-1]
    assert it.agent_session_id == session.agent_session_id

    refreshed = runtime.candidate_sessions.session(session.agent_session_id, run_id=run_id)
    assert refreshed.counters.get("verifier_runs") == 1


def test_run_verifier_without_agent_session_id_is_main_final_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "inner_agent": "autoresearch",
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    candidate_id = tasks[0].candidate_id
    runtime.start_agent_session(run_id, candidate_id, {"goal": "iterate"})

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"combined_score": 0.6, "valid": true}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)
    # Main final verify call - no agent_session_id, no auto-attribution.
    report = runtime.run_verifier(run_id, candidate_id)
    assert report.aggregate_score == 0.6

    record = runtime.candidate_creation.state.read_candidate(run_id, candidate_id)
    it = record.iterations[-1]
    assert it.agent_session_id is None


def test_removed_runtime_methods_are_absent() -> None:
    """Defensive guardrail: lifecycle/observation methods must not be
    reintroduced on the runtime."""
    for name in (
        "update_agent_status",
        "list_agent_status",
        "sync_host_agent_sessions",
        "_finish_agent_session_from_host",
        "_host_observation_reason",
        "finish_agent_session",
        "abort_agent_session",
        "abort_all_agent_sessions",
        "_abort_agent_session_record",
        "publish_observation",
        "list_observations",
        "wait_agent_events",
        "_active_agent_session_count",
        "_append_agent_event",
        "_write_agent_event",
        "_load_agent_events",
        "submit_candidate",
        "next_batch",
    ):
        assert not hasattr(FileSearchRuntime, name), (
            f"FileSearchRuntime.{name} should be removed"
        )


def test_run_verifier_parses_subprocess_metrics_with_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, workspace = create_candidate(runtime, project)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='debug line\n{"combined_score": 0.75, "valid": true}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)

    report = runtime.run_verifier(run_id, candidate_id)

    assert report.process_passed is True
    assert report.aggregate_score == 0.75
    assert calls[0][1]["cwd"] == workspace.resolve()
    assert "PYTHONPATH" in calls[0][1]["env"]


@pytest.mark.parametrize(
    ("metric_name", "metric_value"),
    [("elapsed_seconds", 123.0), ("returncode", 17.0)],
)
def test_run_verifier_preserves_ranking_metric_named_like_runtime_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metric_name: str,
    metric_value: float,
) -> None:
    project = make_project(tmp_path)
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["metric_name"] = metric_name
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    candidate_id = runtime.start_batch(run_id, plan.plan_id)[0].candidate_id

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps({metric_name: metric_value}) + "\n",
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)

    report = runtime.run_verifier(run_id, candidate_id)

    assert report.aggregate_score == metric_value
    assert report.verifier_results[0].metrics[metric_name] == metric_value


def test_process_verifier_overrides_verifier_phase_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, _workspace = create_candidate(runtime, project)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"combined_score": 0.75}\n',
            stderr="",
        )

    monkeypatch.setenv("GOAL_PLUS_VERIFIER_PHASE", "caller_value")
    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)

    report = runtime.run_verifier(run_id, candidate_id, scope="process")

    assert report.process_passed is True
    assert calls[0][1]["env"]["GOAL_PLUS_VERIFIER_PHASE"] == "candidate"


def test_candidates_receive_stable_device_resources_and_frozen_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    spec_data = spec_for(project, max_parallel=2).model_dump(mode="json")
    spec_data["candidate_acceptance"] = [
        "Preserve output correctness across the declared input shapes.",
        "Improve median latency under the frozen verifier.",
    ]
    spec_data["constraints"] = {
        "baseline_median_latency_ms": 6.5,
        "target_median_latency_ms": 5.2,
        "private_runtime_note": "must-not-reach-candidate",
    }
    spec_data["process_verifiers"][0]["resource_lock"] = "device:0"
    spec_data["device_pools"] = [{"pool_id": "device"}]
    monkeypatch.setenv(
        DEVICE_POOLS_ENV,
        canonical_json(
            {
                "device": {
                    "visibility_env": "TEST_VISIBLE_DEVICES",
                    "devices": ["4", "5"],
                }
            }
        ),
    )
    monkeypatch.setenv(
        VERIFIER_RESOURCE_LOCK_DIR_ENV,
        str(tmp_path / "host-resource-locks"),
    )

    runtime = FileSearchRuntime(tmp_path / ".search")
    calls: list[dict[str, str]] = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs["env"])
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"combined_score": 0.75}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    monkeypatch.setenv(
        DEVICE_POOLS_ENV,
        canonical_json(
            {
                "device": {
                    "visibility_env": "TEST_VISIBLE_DEVICES",
                    "devices": ["8", "9"],
                }
            }
        ),
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    assert [task.process_environment for task in tasks] == [
        {"TEST_VISIBLE_DEVICES": device} for device in ("4", "5")
    ]
    assert frozen.device_inventory["device"].model_dump(mode="json") == {
        "visibility_env": "TEST_VISIBLE_DEVICES",
        "devices": ["4", "5"],
    }
    assert tasks[0].acceptance == spec_data["candidate_acceptance"]
    for task in tasks:
        runtime.run_verifier(run_id, task.candidate_id)

    assert calls[0]["GOAL_PLUS_VERIFIER_RESOURCE"] == "device:4"
    assert calls[0]["TEST_VISIBLE_DEVICES"] == "4"
    assert [env["TEST_VISIBLE_DEVICES"] for env in calls[1:]] == [
        "4",
        "5",
    ]
    assert [env["GOAL_PLUS_VERIFIER_RESOURCE"] for env in calls[1:]] == [
        "device:4",
        "device:5",
    ]

    session = runtime.start_agent_session(run_id, tasks[1].candidate_id)
    runtime.bind_agent_handle(
        session.agent_session_id,
        {
            "host": "codex",
            "external_id": "native-c002",
            "task_name": session.launch["task_name"],
        },
    )
    runtime.continue_agent_session(session.agent_session_id)
    context = runtime.get_agent_context(session.agent_session_id)
    assert context["candidate_task"]["acceptance"] == tasks[1].acceptance
    assert context["objective"] == frozen.spec.objective
    assert context["metric_name"] == "combined_score"
    assert "must-not-reach-candidate" not in canonical_json(context)
    assert context["candidate_task"]["process_environment"] == {
        "TEST_VISIBLE_DEVICES": "5"
    }


@pytest.mark.parametrize(
    ("pools", "inventory", "max_parallel", "message"),
    [
        (
            [DevicePoolSpec(pool_id="device")],
            {
                "device": {
                    "visibility_env": "VISIBLE_DEVICES",
                    "devices": ["0", "1"],
                }
            },
            3,
            "fewer than budget.max_parallel=3",
        ),
        (
            [DevicePoolSpec(pool_id="first"), DevicePoolSpec(pool_id="second")],
            {
                "first": {"visibility_env": "VISIBLE_DEVICES", "devices": ["0"]},
                "second": {"visibility_env": "VISIBLE_DEVICES", "devices": ["1"]},
            },
            1,
            "use the same visibility_env",
        ),
    ],
)
def test_device_inventory_rejects_oversubscription_and_ambiguous_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pools: list[DevicePoolSpec],
    inventory: dict[str, object],
    max_parallel: int,
    message: str,
) -> None:
    monkeypatch.setenv(DEVICE_POOLS_ENV, canonical_json(inventory))

    with pytest.raises(ValueError, match=message):
        SearchConfiguration.resolve_device_inventory(
            pools,
            max_parallel,
        )


def test_device_pool_lease_releases_after_terminal_or_create_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["process_verifiers"][0]["resource_lock"] = "device:0"
    spec_data["device_pools"] = [{"pool_id": "device"}]
    monkeypatch.setenv(
        DEVICE_POOLS_ENV,
        canonical_json(
            {
                "device": {
                    "visibility_env": "VISIBLE_DEVICES",
                    "devices": ["0"],
                }
            }
        ),
    )
    monkeypatch.setenv(
        VERIFIER_RESOURCE_LOCK_DIR_ENV,
        str(tmp_path / "host-resource-locks"),
    )

    runtimes = [
        FileSearchRuntime(tmp_path / ".search-a"),
        FileSearchRuntime(tmp_path / ".search-b"),
    ]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"combined_score": 0.75}\n',
            stderr="",
        )

    for runtime in runtimes:
        monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)
    frozen_specs = [
        runtime.freeze_spec(
            SearchSpec.model_validate(spec_data),
            [project / "evaluator.py"],
        )
        for runtime in runtimes
    ]

    first_run = runtimes[0].create_run(frozen_specs[0].frozen_spec_id)
    with pytest.raises(RuntimeError, match="device pool 'device' is leased"):
        runtimes[1].create_run(frozen_specs[1].frozen_spec_id)

    runtimes[0].invalidate_run(
        first_run,
        reason="verifier_contract_invalid",
        summary="release test lease",
        evidence=[{"source": "unit test"}],
    )

    @contextmanager
    def fail_source_update(run_id: str):
        if run_id == first_run:
            raise RuntimeError("source update failed")
        yield

    monkeypatch.setattr(runtimes[0].search_run.state, "lock_run", fail_source_update)
    with pytest.raises(RuntimeError, match="source update failed"):
        runtimes[0].create_run(
            frozen_specs[0].frozen_spec_id,
            source_run_id=first_run,
        )

    failed_run_id = next(
        path.parent.name
        for path in runtimes[0].runs_dir.glob("run_*/run.json")
        if path.parent.name != first_run
    )
    assert runtimes[0].status(failed_run_id).state == RunState.FAILED
    assert runtimes[1].create_run(frozen_specs[1].frozen_spec_id).startswith("run_")


def test_device_pool_ref_does_not_read_an_unregistered_environment_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DEVICE_POOLS_ENV, raising=False)
    monkeypatch.setenv("HOST_SECRET", "must-not-be-exposed")

    with pytest.raises(ValueError, match=DEVICE_POOLS_ENV) as error:
        SearchConfiguration.resolve_device_inventory(
            [DevicePoolSpec(pool_id="HOST_SECRET")],
            1,
        )

    assert "must-not-be-exposed" not in str(error.value)


def test_verifier_resource_lock_serializes_candidates_and_persists_diagnostics(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    marker = tmp_path / "device-active"
    resource = f"test-device:{tmp_path.name}"
    (project / "evaluator.py").write_text(
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "import time\n"
        f"marker = Path({str(marker)!r})\n"
        f"assert os.environ.get('GOAL_PLUS_VERIFIER_RESOURCE') == {resource!r}\n"
        "if marker.exists():\n"
        "    raise SystemExit('concurrent verifier reached exclusive resource')\n"
        "marker.write_text('active')\n"
        "try:\n"
        "    time.sleep(0.15)\n"
        "    diagnostics = Path(os.environ['GOAL_PLUS_VERIFIER_DIAGNOSTICS_DIR'])\n"
        "    diagnostics.joinpath('official-result.json').write_text('{}\\n')\n"
        "    print(json.dumps({'combined_score': 1.0}))\n"
        "finally:\n"
        "    marker.unlink(missing_ok=True)\n",
        encoding="utf-8",
    )
    spec_data = spec_for(project, max_parallel=2).model_dump(mode="json")
    spec_data["process_verifiers"][0]["resource_lock"] = resource
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(
            executor.map(
                lambda task: runtime.run_verifier(run_id, task.candidate_id),
                tasks,
            )
        )

    assert all(report.process_passed for report in reports)
    for report in reports:
        metrics = report.verifier_results[0].metrics
        diagnostics = Path(metrics["diagnostics_dir"])
        assert metrics["diagnostic_files"] == ["official-result.json"]
        assert diagnostics.joinpath("official-result.json").is_file()


def test_promotion_verifier_overrides_verifier_phase_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    spec_data = spec_for(project).model_dump(mode="json")
    spec_data["promotion_verifiers"] = [
        {
            "name": "promotion",
            "role": "promotion_gate",
            "command": ["python", "evaluator.py"],
            "timeout_seconds": 30,
        }
    ]
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    runtime.run_verifier(run_id, task.candidate_id)
    runtime.select(run_id)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"combined_score": 0.75}\n',
            stderr="",
        )

    monkeypatch.setenv("GOAL_PLUS_VERIFIER_PHASE", "caller_value")
    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)

    report = runtime.run_verifier(
        run_id,
        task.candidate_id,
        scope="promotion",
    )

    assert report.promotion_passed is True
    assert calls[0][1]["env"]["GOAL_PLUS_VERIFIER_PHASE"] == "promotion"


def test_run_verifier_handles_subprocess_timeout_with_mock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, _workspace = create_candidate(runtime, project)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)

    report = runtime.run_verifier(run_id, candidate_id)

    assert report.process_passed is False
    assert report.aggregate_score == 0.0
    assert report.verifier_results[0].failure_class == "Timeout"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_verifier_timeout_terminates_entire_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    child_pid_path = tmp_path / "grandchild.pid"
    monkeypatch.setenv("GOAL_PLUS_TEST_CHILD_PID_PATH", str(child_pid_path))
    (project / "evaluator.py").write_text(
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "import signal\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "if os.environ.get('GOAL_PLUS_VERIFIER_PHASE') == 'freeze_preflight':\n"
        "    print(json.dumps({'combined_score': 0.0}))\n"
        "    raise SystemExit(0)\n"
        "child_code = (\n"
        "    \"import os, signal, time; from pathlib import Path; \"\n"
        "    \"signal.signal(signal.SIGTERM, signal.SIG_IGN); \"\n"
        "    \"Path(os.environ['GOAL_PLUS_TEST_CHILD_PID_PATH']).write_text(str(os.getpid())); \"\n"
        "    \"time.sleep(60)\"\n"
        ")\n"
        "subprocess.Popen([sys.executable, '-c', child_code])\n"
        "deadline = time.time() + 5\n"
        "pid_path = Path(os.environ['GOAL_PLUS_TEST_CHILD_PID_PATH'])\n"
        "while not pid_path.exists() and time.time() < deadline:\n"
        "    time.sleep(0.01)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["process_verifiers"][0]["timeout_seconds"] = 1
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]

    report = runtime.run_verifier(run_id, task.candidate_id)

    assert report.verifier_results[0].failure_class == "Timeout"
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3
    while process_is_running(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not process_is_running(child_pid)


def test_verifier_logs_keep_bounded_output_tails(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / "evaluator.py").write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "if os.environ.get('GOAL_PLUS_VERIFIER_PHASE') == 'freeze_preflight':\n"
        "    print(json.dumps({'combined_score': 0.0}))\n"
        "else:\n"
        f"    print('x' * {VERIFIER_OUTPUT_LIMIT_BYTES * 3})\n"
        f"    print('y' * {VERIFIER_OUTPUT_LIMIT_BYTES * 3}, file=sys.stderr)\n"
        "    print(json.dumps({'combined_score': 0.5}))\n",
        encoding="utf-8",
    )
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, _workspace = create_candidate(runtime, project)

    report = runtime.run_verifier(run_id, candidate_id)

    assert report.process_passed is True
    log_path = report.verifier_results[0].log_path
    assert log_path is not None
    log_text = log_path.read_text(encoding="utf-8")
    assert log_text.count("[... output truncated ...]") == 2
    assert log_path.stat().st_size < VERIFIER_OUTPUT_LIMIT_BYTES * 2 + 4096


def test_select_uses_metric_direction_for_minimize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=2, direction="minimize"),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    for task in tasks:
        runtime.start_agent_session(run_id, task.candidate_id, {"goal": "submit"})

    def fake_run(*args, **kwargs):
        cwd = Path(kwargs["cwd"])
        score = 0.1 if cwd.name == "c002" else 0.9
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=f'{{"combined_score": {score}}}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)
    runtime.run_verifier(run_id, "c001")
    runtime.run_verifier(run_id, "c002")

    selection = runtime.select(run_id)

    assert selection["selected_candidate_id"] == "c002"
    assert selection["selected_score"] == 0.1


def test_select_does_not_reverify_duplicate_latest_artifact_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=2),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    runtime.start_batch(run_id, plan.plan_id)
    calls = {"c001": 0, "c002": 0}

    def fake_run(*args, **kwargs):
        candidate_id = Path(kwargs["cwd"]).name
        calls[candidate_id] += 1
        if candidate_id == "c001" and calls[candidate_id] > 1:
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=20,
                stdout='{"outcome": "infrastructure_failure"}\n',
                stderr="",
            )
        score = 0.9 if candidate_id == "c001" else 0.8
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=f'{{"combined_score": {score}}}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)
    runtime.run_verifier(run_id, "c001")
    runtime.run_verifier(run_id, "c002")

    selection = runtime.select(run_id)

    assert selection["selected_candidate_id"] == "c002"
    assert calls == {"c001": 2, "c002": 2}


def test_select_reuses_exact_worker_evidence_without_parent_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=1), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"combined_score": 0.9}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="worker attempt",
    )

    selection = runtime.select(run_id)

    assert selection["selected_candidate_id"] == task.candidate_id
    assert selection["selected_score"] == 0.9
    assert calls == 1
    assert runtime.promote(run_id, task.candidate_id).exists()
    assert calls == 1


def test_select_uses_best_iteration_when_artifact_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=2),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    for task in tasks:
        (task.workspace / "initial_program.py").write_text(
            f"VALUE = {task.candidate_id!r}\n", encoding="utf-8"
        )

    scores_by_candidate = {
        "c001": [0.9, 0.4, 0.9],
        "c002": [0.7],
    }
    real_run = subprocess.run

    def fake_run(*args, **kwargs):
        command = args[0]
        if command and command[0] != "python":
            return real_run(*args, **kwargs)
        candidate_id = Path(kwargs["cwd"]).name
        score = scores_by_candidate[candidate_id].pop(0)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=f'{{"combined_score": {score}, "valid": true}}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)

    runtime.run_verifier(run_id, "c001")
    runtime.run_verifier(run_id, "c001")
    runtime.run_verifier(run_id, "c002")

    selection = runtime.select(run_id)

    assert selection["selected_candidate_id"] == "c001"
    assert selection["selected_score"] == 0.9
    assert selection["selected_iteration"] == 1


def test_select_can_recover_best_iteration_after_artifact_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=2),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    scores_by_candidate = {
        "c001": [0.9, 0.4, 0.9],
        "c002": [0.7, 0.7],
    }
    real_run = subprocess.run

    def fake_run(*args, **kwargs):
        command = args[0]
        if command and command[0] != "python":
            return real_run(*args, **kwargs)
        candidate_id = Path(kwargs["cwd"]).name
        score = scores_by_candidate[candidate_id].pop(0)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=f'{{"combined_score": {score}, "valid": true}}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)

    c001_workspace = tasks[0].workspace
    c001_workspace.joinpath("initial_program.py").write_text(
        "VALUE = 'fast'\n", encoding="utf-8"
    )
    runtime.run_verifier(run_id, "c001")
    c001_workspace.joinpath("initial_program.py").write_text(
        "VALUE = 'slow'\n", encoding="utf-8"
    )
    runtime.run_verifier(run_id, "c001")

    tasks[1].workspace.joinpath("initial_program.py").write_text(
        "VALUE = 'middle'\n", encoding="utf-8"
    )
    runtime.run_verifier(run_id, "c002")

    selection = runtime.select(run_id)

    assert selection["selected_candidate_id"] == "c001"
    assert selection["selected_iteration"] == 1
    assert selection["selected_score"] == 0.9
    assert tasks[0].workspace.joinpath("initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 'fast'\n"


def test_run_verifier_rejects_missing_results_ledger_git_history(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(".search/\n", encoding="utf-8")
    project = make_project(tmp_path)
    _initialize_source_repository(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    results_before = tasks[0].workspace.joinpath("results.tsv").read_text(
        encoding="utf-8"
    )
    source_head_before = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    shutil.rmtree(tasks[0].workspace / ".git")

    with pytest.raises(RuntimeError, match="ResultsLedgerMutation"):
        runtime.run_verifier(run_id, tasks[0].candidate_id)

    assert tasks[0].workspace.joinpath("results.tsv").read_text(
        encoding="utf-8"
    ) == results_before
    assert runtime.list_iterations(run_id, tasks[0].candidate_id) == []
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip() == source_head_before
    assert subprocess.check_output(
        ["git", "status", "--porcelain=v1"], cwd=tmp_path, text=True
    ) == ""


def test_run_verifier_records_real_git_commit_for_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, workspace = create_candidate(runtime, project)
    workspace.joinpath("initial_program.py").write_text(
        "VALUE = 'committed'\n", encoding="utf-8"
    )

    real_run = subprocess.run

    def fake_run(*args, **kwargs):
        command = args[0]
        if command and command[0] != "python":
            return real_run(*args, **kwargs)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='{"combined_score": 0.9, "valid": true}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)

    runtime.run_verifier(run_id, candidate_id)

    iteration = runtime.list_iterations(run_id, candidate_id)[0]
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
    ).strip()
    assert iteration["ledger_git_head"] == head
    assert iteration["git_head"] != head
    assert iteration["git_artifact_clean"] is True


def test_results_tsv_is_committed_and_runtime_enforces_one_row_per_report(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, workspace = create_candidate(runtime, project)
    results_path = workspace / "results.tsv"

    assert results_path.read_text(encoding="utf-8") == (
        "commit\tcombined_score\tstatus\thypothesis\n"
    )
    assert not (workspace / ".tmp" / "results.tsv").exists()
    assert subprocess.check_output(
        ["git", "ls-files", "--", "results.tsv"],
        cwd=workspace,
        text=True,
    ).strip() == "results.tsv"
    assert subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--", "results.tsv"],
        cwd=workspace,
        text=True,
    ).strip() == ""

    session = runtime.start_agent_session(run_id, candidate_id)
    first = runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="measure inherited baseline",
    )
    assert first.process_passed is True
    first_text = results_path.read_text(encoding="utf-8")
    assert len(first_text.splitlines()) == 2
    assert first_text.splitlines()[1].endswith(
        "\tpass\tmeasure inherited baseline"
    )

    (workspace / "config.yaml").write_text("name: denied edit\n", encoding="utf-8")
    second = runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="probe denied configuration change",
    )
    assert second.process_passed is False
    second_text = results_path.read_text(encoding="utf-8")
    assert second_text.startswith(first_text)
    assert len(second_text.splitlines()) == 3
    assert second_text.splitlines()[2].endswith(
        "\tfail\tprobe denied configuration change"
    )

    record = runtime.candidate_creation.state.read_candidate(run_id, candidate_id)
    assert len(record.iterations) == 2
    assert len(record.results_ledger) == 2
    assert record.iterations[-1].ledger_git_head == record.results_ledger_git_head
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
    ).strip() == record.results_ledger_git_head

    redispatched = runtime.redispatch_candidate(run_id, candidate_id)
    context = runtime.get_agent_context(redispatched.agent_session_id)
    assert context["results_tsv"] == str(results_path)
    assert context["result_count"] == 2
    assert context["latest_result"]["hypothesis"] == (
        "probe denied configuration change"
    )

    report = runtime.report(run_id).read_text(encoding="utf-8")
    relative_results = f"workspace/{candidate_id}/results.tsv"
    assert "## Results Ledgers" in report
    assert f"[results.tsv]({relative_results}) (2 rows)" in report
    assert f"[results.tsv]({relative_results})" in report
    assert "probe denied configuration change" in report

    results_path.write_text(
        second_text.replace("measure inherited baseline", "rewritten baseline"),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="ResultsLedgerMutation"):
        runtime.run_verifier(run_id, candidate_id, hypothesis="must not run")
    assert len(runtime.candidate_creation.state.read_candidate(run_id, candidate_id).results_ledger) == 2


def test_results_tsv_inherits_across_successor_run(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(project, {"name": "random"}, max_parallel=1)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    first_run_id = runtime.create_run(frozen.frozen_spec_id)

    first_plan = runtime.plan_next(first_run_id, requested_k=1)
    parent = runtime.start_batch(first_run_id, first_plan.plan_id)[0]
    runtime.run_verifier(
        first_run_id,
        parent.candidate_id,
        hypothesis="parent design",
    )
    parent_results = parent.workspace.joinpath("results.tsv").read_text(
        encoding="utf-8"
    )

    successor_run_id = runtime.create_run(
        frozen.frozen_spec_id,
        source_run_id=first_run_id,
    )
    successor_plan = runtime.plan_next(successor_run_id, requested_k=1)
    successor = runtime.start_batch(successor_run_id, successor_plan.plan_id)[0]
    assert successor.workspace.joinpath("results.tsv").read_text(
        encoding="utf-8"
    ) == parent_results
    successor_record = runtime.candidate_creation.state.read_candidate(
        successor_run_id,
        successor.candidate_id,
    )
    assert [entry.source_run_id for entry in successor_record.results_ledger] == [
        first_run_id
    ]
    assert subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--", "results.tsv"],
        cwd=successor.workspace,
        text=True,
    ).strip() == ""


def test_legacy_tmp_results_tsv_migrates_and_backfills_missing_iterations(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    run_id, candidate_id, workspace = create_candidate(runtime, project)
    runtime.run_verifier(run_id, candidate_id, hypothesis="legacy row kept")
    runtime.run_verifier(run_id, candidate_id, hypothesis="missing legacy row")

    current_lines = workspace.joinpath("results.tsv").read_text(
        encoding="utf-8"
    ).splitlines()
    legacy_path = workspace / ".tmp" / "results.tsv"
    legacy_path.write_text("\n".join(current_lines[:2]) + "\n", encoding="utf-8")
    workspace.joinpath("results.tsv").unlink()

    candidate_path = runtime.candidate_creation.state.candidate_dir(run_id, candidate_id) / "candidate.json"
    legacy_record = load_json(candidate_path)
    legacy_record.pop("results_ledger", None)
    legacy_record.pop("results_ledger_git_head", None)
    legacy_record.pop("results_ledger_artifact_ref", None)
    for iteration in legacy_record["iterations"]:
        iteration.pop("ledger_git_head", None)
        iteration.pop("ledger_artifact_ref", None)
        iteration.pop("workspace_artifact_after_settlement", None)
        iteration.pop("hypothesis", None)
        iteration["summary"] = ""
    write_json(candidate_path, legacy_record)

    session = runtime.start_agent_session(run_id, candidate_id)
    context = runtime.get_agent_context(session.agent_session_id)
    migrated_lines = workspace.joinpath("results.tsv").read_text(
        encoding="utf-8"
    ).splitlines()

    assert len(migrated_lines) == 3
    assert migrated_lines[1] == current_lines[1]
    assert migrated_lines[2].endswith("\tpass\trecovered iteration 2")
    assert context["result_count"] == 2
    assert context["latest_result"]["hypothesis"] == "recovered iteration 2"
    migrated_record = runtime.candidate_creation.state.read_candidate(run_id, candidate_id)
    assert migrated_record.results_ledger_git_head is not None
    assert len(migrated_record.results_ledger) == 2


def test_select_restores_best_git_commit_before_final_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        spec_for(project, max_parallel=2),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    c001_workspace = tasks[0].workspace
    c002_workspace = tasks[1].workspace

    real_run = subprocess.run

    def fake_run(*args, **kwargs):
        command = args[0]
        if command and command[0] == "python":
            content = Path(kwargs["cwd"], "initial_program.py").read_text(
                encoding="utf-8"
            )
            score = 0.9 if "fast" in content else 0.4 if "slow" in content else 0.7
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=f'{{"combined_score": {score}, "valid": true}}\n',
                stderr="",
            )
        return real_run(*args, **kwargs)

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)

    c001_workspace.joinpath("initial_program.py").write_text(
        "VALUE = 'fast'\n", encoding="utf-8"
    )
    fast_commit = git_commit_all(c001_workspace, "fast version")
    runtime.run_verifier(run_id, "c001")

    c001_workspace.joinpath("initial_program.py").write_text(
        "VALUE = 'slow'\n", encoding="utf-8"
    )
    git_commit_all(c001_workspace, "slow version")
    runtime.run_verifier(run_id, "c001")

    c002_workspace.joinpath("initial_program.py").write_text(
        "VALUE = 'middle'\n", encoding="utf-8"
    )
    git_commit_all(c002_workspace, "middle version")
    runtime.run_verifier(run_id, "c002")

    selection = runtime.select(run_id)

    assert selection["selected_candidate_id"] == "c001"
    assert selection["selected_iteration"] == 1
    assert selection["selected_git_head"] == fast_commit
    assert selection["selected_score"] == 0.9
    assert c001_workspace.joinpath("initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 'fast'\n"
    final_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=c001_workspace, text=True
    ).strip()
    selected_record = runtime.candidate_creation.state.read_candidate(run_id, "c001")
    assert final_head == selected_record.results_ledger_git_head
    assert final_head != fast_commit


def test_run_verifier_rejects_mismatched_agent_session(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
        },
        max_parallel=2,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    session_for_c0 = runtime.start_agent_session(
        run_id, tasks[0].candidate_id, {"goal": "c0"}
    )
    other_session = runtime.start_agent_session(
        run_id, tasks[1].candidate_id, {"goal": "c1"}
    )
    state_before = {
        "target": _guarded_runtime_state(
            runtime,
            run_id,
            tasks[0].candidate_id,
            session_for_c0.agent_session_id,
        ),
        "other_session": runtime.candidate_sessions.session(
            other_session.agent_session_id,
            run_id=run_id,
        ).model_dump(mode="json"),
    }

    with pytest.raises(ValueError, match="agent_session_id does not belong"):
        runtime.run_verifier(
            run_id,
            tasks[0].candidate_id,
            agent_session_id=other_session.agent_session_id,
        )

    assert {
        "target": _guarded_runtime_state(
            runtime,
            run_id,
            tasks[0].candidate_id,
            session_for_c0.agent_session_id,
        ),
        "other_session": runtime.candidate_sessions.session(
            other_session.agent_session_id,
            run_id=run_id,
        ).model_dump(mode="json"),
    } == state_before


def test_concurrent_run_verifiers_preserve_best_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
        },
        max_parallel=2,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    both_verifiers_started = threading.Barrier(2)
    high_score_committed = threading.Event()
    errors: list[BaseException] = []

    def fake_run(*args, **kwargs):
        cwd = Path(kwargs["cwd"])
        both_verifiers_started.wait(timeout=5)
        if cwd.name == "c002":
            assert high_score_committed.wait(timeout=5)
            score = 0.1
        else:
            score = 0.9
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=f'{{"combined_score": {score}}}\n',
            stderr="",
        )

    def verify(candidate_id: str) -> None:
        try:
            runtime.run_verifier(run_id, candidate_id)
            if candidate_id == "c001":
                high_score_committed.set()
        except BaseException as exc:  # pragma: no cover - surfaced after join
            errors.append(exc)

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)

    high = threading.Thread(target=verify, args=(tasks[0].candidate_id,))
    low = threading.Thread(target=verify, args=(tasks[1].candidate_id,))
    high.start()
    low.start()
    high.join(timeout=10)
    low.join(timeout=10)

    assert not high.is_alive()
    assert not low.is_alive()
    assert errors == []

    run = runtime.search_run.state.read_run(run_id)
    assert run.best_candidate_id == "c001"
    assert run.best_score == 0.9
    assert run.candidates_evaluated == 2


def test_run_verifier_works_without_session_and_records_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "inner_agent": "autoresearch",
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    candidate_id = tasks[0].candidate_id
    session = runtime.start_agent_session(run_id, candidate_id, {"goal": "iterate"})

    scores = [0.4, 0.7, 0.9]

    def fake_run(*args, **kwargs):
        score = scores.pop(0)
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=f'{{"combined_score": {score}, "valid": true}}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)

    for iteration_number, expected_score in enumerate([0.4, 0.7, 0.9], start=1):
        report = runtime.run_verifier(
            run_id,
            candidate_id,
            agent_session_id=session.agent_session_id,
            hypothesis=f"Measure score sample {iteration_number}",
        )
        assert report.aggregate_score == expected_score

    record = runtime.candidate_creation.state.read_candidate(run_id, candidate_id)
    assert len(record.iterations) == 3
    assert [it.score for it in record.iterations] == [0.4, 0.7, 0.9]
    assert [it.iteration for it in record.iterations] == [1, 2, 3]
    assert record.score_report.aggregate_score == 0.9  # type: ignore[union-attr]


def test_list_iterations_returns_all_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    candidate_id = tasks[0].candidate_id
    session = runtime.start_agent_session(run_id, candidate_id, {"goal": "iterate"})

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"combined_score": 0.5, "valid": true}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)
    runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="First listed iteration",
    )
    runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="Second listed iteration",
    )

    iterations = runtime.list_iterations(run_id, candidate_id)
    assert len(iterations) == 2
    assert iterations[0]["iteration"] == 1
    assert iterations[1]["iteration"] == 2
    assert all(it["agent_session_id"] == session.agent_session_id for it in iterations)


def test_get_agent_context_returns_bounded_iteration_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    candidate_id = tasks[0].candidate_id
    session = runtime.start_agent_session(run_id, candidate_id, {"goal": "iterate"})

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"combined_score": 0.42, "valid": true}\n',
            stderr="",
        )

    monkeypatch.setattr(runtime.verifiers, "execute_verifier_process", fake_run)
    runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="Context iteration",
    )

    for iteration_number in range(2, 6):
        runtime.run_verifier(
            run_id,
            candidate_id,
            agent_session_id=session.agent_session_id,
            hypothesis=f"Context iteration {iteration_number}",
        )

    context = runtime.get_agent_context(session.agent_session_id)
    assert "iterations" not in context
    assert context["iteration_count"] == 5
    assert [item["iteration"] for item in context["recent_iterations"]] == [3, 4, 5]
    assert context["best_iteration"]["iteration"] == 5
    assert context["best_iteration"]["score"] == 0.42
    assert "metrics" not in context["recent_iterations"][-1]


def test_history_and_report_include_agent_sessions(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "inner_agent": "autoresearch",
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id, {"goal": "document session"})

    history = runtime.list_history(run_id)
    candidate = history["candidates"][0]
    assert candidate["agent_sessions"][0]["agent_session_id"] == session.agent_session_id

    report_path = runtime.report(run_id)
    report = report_path.read_text(encoding="utf-8")
    assert "## Agent Sessions" in report
    assert session.agent_session_id in report


@pytest.mark.parametrize("goal_status", ["active", "needs_user"])
def test_report_rejects_linked_nonterminal_goal(
    tmp_path: Path,
    goal_status: str,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    goal_dir = runtime.root_dir / "goal-plus" / "gp_report"
    goal_dir.mkdir(parents=True)
    write_json(
        goal_dir / "goal.json",
        {
            "goal_plus_id": "gp_report",
            "status": goal_status,
            "search_tasks": [{"run_id": run_id}],
        },
    )
    run_before = runtime.search_run.state.read_run(run_id)

    with pytest.raises(RuntimeError, match="reaches a terminal status"):
        runtime.report(run_id)

    assert runtime.search_run.state.read_run(run_id) == run_before
    assert not (runtime.search_run.state.run_dir(run_id) / "report.md").exists()
    assert not (runtime.search_run.state.run_dir(run_id) / "report.html").exists()


@pytest.mark.parametrize("goal_status", ["complete", "blocked", "abandoned"])
def test_report_allows_linked_terminal_goal(
    tmp_path: Path,
    goal_status: str,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    goal_dir = runtime.root_dir / "goal-plus" / "gp_report"
    goal_dir.mkdir(parents=True)
    write_json(
        goal_dir / "goal.json",
        {
            "goal_plus_id": "gp_report",
            "status": goal_status,
            "search_tasks": [{"run_id": run_id}],
        },
    )

    report_path = runtime.report(run_id)

    assert report_path.is_file()
    assert report_path.with_suffix(".html").is_file()


@pytest.mark.pi
def test_history_projects_latest_structured_research_handoff(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "worker_budget": {"max_runtime_seconds": 600},
        },
        max_parallel=1,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    runtime.bind_agent_handle(
        session.agent_session_id,
        {
            "host": "pi-rpc",
            "external_id": "pi-session-1",
            "metadata": {
                "progress_handoff": {
                    "summary": "reworked scratch residency",
                    "model_handoff": {
                        "summary": "reworked scratch residency",
                        "key_results": [
                            {
                                "artifact": "iteration 3",
                                "code_surface": "kernel.py:build_schedule",
                                "change": "keep scratch values resident",
                                "portability": "standalone",
                                "depends_on": [],
                                "measured_effect": "score 5.0 -> 7.0",
                                "verifier_result": "score 7.0",
                                "relation_to_incumbent": "orthogonal",
                                "conclusion": "batch reuse is promising",
                            }
                        ],
                        "pitfalls": [
                            {
                                "condition": "when the gather spans six lanes",
                                "failed_approach": "fully interleave all loads",
                                "reason": "scratch pressure causes spills",
                                "recommendation": "keep two lanes staged",
                            }
                        ],
                        "blockers": ["no cheap slot-occupancy probe"],
                        "next_steps": ["test four-way interleave"],
                        "verifier_assessment": {
                            "status": "adequate",
                            "evidence": ["local ranking is deterministic"],
                            "impact": "safe to compare variants",
                            "recommended_action": "keep_spec",
                        },
                    },
                }
            },
        },
    )

    history = runtime.list_history(run_id)
    candidate = history["candidates"][0]

    assert candidate["summary"] == "reworked scratch residency"
    assert candidate["key_results"][0]["artifact"] == "iteration 3"
    assert candidate["feature_ledger"][0]["code_surface"] == (
        "kernel.py:build_schedule"
    )
    assert candidate["verifier_assessment"]["status"] == "adequate"
    assert history["feature_ledger"][0]["relation_to_incumbent"] == "orthogonal"
    assert history["verifier_assessments"][0]["candidate_id"] == task.candidate_id
    assert history["research_rollup"]["pitfalls"][0]["scope"] == "candidate_local"
    assert history["pitfalls"] == history["research_rollup"]["pitfalls"]
    assert history["research_rollup"]["pitfalls"][0]["confidence"] == (
        "single_observation"
    )
    assert candidate["risk_notes"][0].startswith(
        "Condition: when the gather spans six lanes; "
        "failed approach: fully interleave all loads"
    )
    assert candidate["research_summary"]["pitfalls"][0]["condition"] == (
        "when the gather spans six lanes"
    )
    assert candidate["blockers"] == ["no cheap slot-occupancy probe"]
    assert candidate["next_ideas"] == ["test four-way interleave"]
    assert candidate["research_summary"]["source_agent_session_id"] == (
        session.agent_session_id
    )


def test_history_feature_ledger_retains_non_frontier_candidate(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "worker_host": "pi-rpc",
            "worker_budget": {"max_runtime_seconds": 600},
        },
        max_parallel=2,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    for task in tasks:
        session = runtime.start_agent_session(run_id, task.candidate_id)
        runtime.bind_agent_handle(
            session.agent_session_id,
            {
                "host": "pi-rpc",
                "external_id": f"pi-{task.candidate_id}",
                "metadata": {
                    "progress_handoff": {
                        "model_handoff": {
                            "summary": f"result from {task.candidate_id}",
                            "key_results": [
                                {
                                    "artifact": "iteration 1",
                                    "code_surface": f"feature-{task.candidate_id}",
                                    "change": "candidate-specific feature",
                                    "portability": "standalone",
                                    "depends_on": [],
                                    "measured_effect": "0.0 -> 1.0",
                                    "verifier_result": "passed",
                                    "relation_to_incumbent": "orthogonal",
                                    "conclusion": "portable",
                                }
                            ],
                            "pitfalls": [],
                            "blockers": [],
                            "next_steps": [],
                            "verifier_assessment": {
                                "status": "unknown",
                                "evidence": [],
                                "impact": "",
                                "recommended_action": "keep_spec",
                            },
                        }
                    }
                },
            },
        )

    history = runtime.list_history(run_id, top_n=1)

    assert history["returned_candidates"] == 1
    assert {entry["candidate_id"] for entry in history["feature_ledger"]} == {
        "c001",
        "c002",
    }
    hidden = next(
        entry for entry in history["feature_ledger"] if entry["candidate_id"] == "c002"
    )
    assert hidden["candidate_visible"] is False


def test_invalidate_run_fences_work_and_successor_inherits_research(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = spec_with_strategy(
        project,
        {"name": "agent_guided"},
        max_parallel=2,
    )
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(
        run_id,
        plan.plan_id,
        [CandidateProposal(intent="bootstrap")],
    )[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    runtime.bind_agent_handle(
        session.agent_session_id,
        {
            "host": "codex",
            "task_name": "search_candidate_c001",
            "metadata": {
                "progress_handoff": {
                    "model_handoff": {
                        "summary": "found a portable fusion",
                        "key_results": [
                            {
                                "artifact": "iteration 1",
                                "code_surface": "kernel.py:hash_stage",
                                "change": "fuse stages 0/2/4",
                                "portability": "standalone",
                                "depends_on": [],
                                "measured_effect": "1.0 -> 2.0",
                                "verifier_result": "passed",
                                "relation_to_incumbent": "orthogonal",
                                "conclusion": "probe against the next incumbent",
                            }
                        ],
                        "pitfalls": [
                            {
                                "scope": "feature_family",
                                "condition": "when all lanes share one scratch bank",
                                "failed_approach": "fully interleave writes",
                                "observed_result": "score regressed",
                                "reason": "bank pressure",
                                "evidence_artifact": "iteration 1",
                                "confidence": "single_observation",
                                "recommendation": "apply only with separate banks",
                            }
                        ],
                        "blockers": [],
                        "next_steps": ["transfer fusion"],
                        "verifier_assessment": {
                            "status": "concern",
                            "evidence": ["required edge case is absent"],
                            "impact": "ranking can accept invalid artifacts",
                            "recommended_action": "upgrade_spec",
                        },
                    }
                }
            },
        },
    )
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="Portable fusion evidence",
    )

    invalidated = runtime.invalidate_run(
        run_id,
        reason="verifier_coverage_inadequate",
        summary="main agent confirmed the missing required edge case",
        evidence=[{"case": "required-edge", "source_candidate_id": "c001"}],
    )

    assert invalidated.state == RunState.ABORTED
    assert invalidated.invalidation_reason == "verifier_coverage_inadequate"
    fenced_state = _guarded_runtime_state(
        runtime,
        run_id,
        task.candidate_id,
        session.agent_session_id,
    )
    with pytest.raises(RuntimeError, match="invalidated"):
        runtime.run_verifier(run_id, task.candidate_id)
    with pytest.raises(RuntimeError):
        runtime.plan_next(run_id, requested_k=1)
    with pytest.raises(RuntimeError):
        runtime.start_agent_session(run_id, task.candidate_id)
    with pytest.raises(RuntimeError):
        runtime.continue_agent_session(session.agent_session_id)
    with pytest.raises(RuntimeError, match="invalidated"):
        runtime.select(run_id)
    with pytest.raises(RuntimeError, match="invalidated"):
        runtime.promote(run_id, task.candidate_id)
    with pytest.raises(RuntimeError, match="invalidated"):
        runtime.stage_shared_tool(
            session.agent_session_id,
            name="late-tool",
            summary="must not be staged after invalidation",
            entrypoint="late.py",
            candidate_relative_source_paths=["initial_program.py"],
        )
    assert (
        _guarded_runtime_state(
            runtime,
            run_id,
            task.candidate_id,
            session.agent_session_id,
        )
        == fenced_state
    )

    successor_id = runtime.create_run(
        frozen.frozen_spec_id,
        source_run_id=run_id,
    )
    successor_history = runtime.list_history(successor_id)
    inherited = successor_history["inherited_research"]

    assert successor_history["source_run_id"] == run_id
    assert inherited["frontier"][0]["candidate_id"] == "c001"
    assert inherited["feature_ledger"][0]["code_surface"] == (
        "kernel.py:hash_stage"
    )
    assert inherited["feature_ledger"][0]["score_reusable"] is False
    assert inherited["pitfalls"][0]["scope"] == "feature_family"
    assert runtime.status(run_id).replacement_run_id == successor_id


def test_invalidation_rejects_in_flight_verifier_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=1), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    original_run_commands = runtime.verifiers.run_commands

    def invalidate_after_execution(*args: object, **kwargs: object) -> ScoreReport:
        report = original_run_commands(*args, **kwargs)  # type: ignore[arg-type]
        runtime.invalidate_run(
            run_id,
            reason="verifier_target_mismatch",
            summary="main agent confirmed target mismatch while verifier ran",
            evidence=[{"target": "hidden judge"}],
        )
        return report

    monkeypatch.setattr(runtime.verifiers, "run_commands", invalidate_after_execution)

    with pytest.raises(RuntimeError, match="record verifier result"):
        runtime.run_verifier(run_id, task.candidate_id)

    assert runtime.status(run_id).state == RunState.ABORTED
    assert runtime.list_iterations(run_id, task.candidate_id) == []


def test_runtime_does_not_create_event_or_observation_dirs(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    runtime.start_agent_session(run_id, tasks[0].candidate_id, {"goal": "iterate"})

    run_dir = runtime.search_run.state.run_dir(run_id)
    assert not (run_dir / "agent_events").exists()
    assert not (run_dir / "observations").exists()
