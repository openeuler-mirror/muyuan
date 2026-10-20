"""Replay selection and exact native delivery boundary contracts."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
from threading import Event

import pytest

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.annotation import EvidenceViewRecord, SupplementalEvaluation
from goal_plus.domain.replay import ReplayBranch, ReplayExperiment, ReplayForkPoint, ReplayTarget
from goal_plus.domain.search import SearchSpec
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.infrastructure import replay_capture
from goal_plus.infrastructure import replay_native
from goal_plus.infrastructure.replay_capture import FileReplayCapture, workspace_digest
from goal_plus.infrastructure import replay_experiment
from goal_plus.infrastructure.replay_experiment import FileReplayExperiments
from goal_plus.infrastructure.replay_native import verified_turn_prefix, verify_injected_prefix
from goal_plus.infrastructure.state.files import write_json
from goal_plus.infrastructure.search_configuration import SUPPLEMENTAL_EVALUATION_ENABLED_ENV
from goal_plus.ports.session import SessionInvocationResult
from goal_plus.runtime import FileSearchRuntime
from goal_plus.workspaces import copy_source_tree
from tests._runtime_helpers import create_candidate, make_project, spec_for, start_host_verified_goal
from tests.test_global_evidence import _search_with_candidates


@pytest.mark.parametrize("provider", ["git_worktree", "copy"])
def test_capture_preserves_visible_detail_and_provider_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str,
) -> None:
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    project = make_project(source_parent)
    root = source_parent / ".gp"
    goal = start_host_verified_goal(root, project)
    runtime = FileSearchRuntime(root)
    spec_data = spec_for(project, max_parallel=2).model_dump(mode="json")
    spec_data["workspace"] = {"provider": provider}
    monkeypatch.setenv(SUPPLEMENTAL_EVALUATION_ENABLED_ENV, "1")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data), [project / "evaluator.py"],
        agent_harness="codex", goal_plus_id=goal.goal_plus_id,
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    from goal_plus.goal_plus import FileGoalPlusRuntime

    FileGoalPlusRuntime(root).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    sessions = [
        runtime.start_agent_session(run_id, task.candidate_id)
        for task in tasks
    ]
    task, peer_task = tasks
    session, peer_session = sessions
    runtime.candidate_sessions.bind_agent_handle(session.agent_session_id, {
        "agent_harness": "codex", "external_id": "thread-source",
        "metadata": {"transport": "app-server"},
    })
    for current_task, current_session, value in zip(tasks, sessions, (1, 2), strict=True):
        (current_task.workspace / "initial_program.py").write_text(
            f"VALUE = {value}\n", encoding="utf-8",
        )
        runtime.run_verifier(
            run_id, current_task.candidate_id,
            agent_session_id=current_session.agent_session_id,
            hypothesis="Make a change",
        )
        annotation = runtime.annotation.task(run_id, current_task.candidate_id, 1)
        assert annotation is not None
        view = EvidenceViewRecord(
            run_id=run_id, candidate_id=current_task.candidate_id, iteration=1,
            attempt_commit=annotation.attempt_commit,
            attempt_artifact_ref=annotation.attempt_artifact_ref,
            description="One useful observation.",
            supplemental_evaluation=SupplementalEvaluation.model_validate({
                "summary": "The change is useful.",
                "dimensions": [{
                    "name": "Correctness", "finding": "The candidate changed the value.",
                    "confidence": "high",
                }],
            }),
            created_at="2026-09-28T00:00:00Z",
        )
        runtime.annotation.tasks.write_task(annotation.model_copy(update={
            "state": "completed", "view": view,
        }))
    rows = runtime.get_global_evidence(session.agent_session_id)
    assert len(rows) == 2 and all(row["supplemental_available"] is True for row in rows)
    opened_before_capture = runtime.open_reference_workspace(
        session.agent_session_id, peer_task.candidate_id, 1,
    )
    assert opened_before_capture.access.root is not None
    assert opened_before_capture.access.root.joinpath(
        "initial_program.py",
    ).read_text(encoding="utf-8") == "VALUE = 2\n"
    monkeypatch.setattr(replay_capture, "execution_scope_for_run", lambda **_kwargs: {
        "native_agent_config": {"model": "test-model"},
    })
    point = FileReplayCapture(root).capture_tool_result(
        agent_session_id=session.agent_session_id,
        tool_name="goal_plus_search_get_global_evidence",
        tool_result=rows, native_thread_id="thread-source",
        native_turn_id="turn-source", native_tool_call_id="exec-source",
        remaining_seconds=300,
    )
    assert point is not None
    assert point.workspace_provider == provider
    assert [(item.candidate_id, item.iteration) for item in point.targets] == [
        (task.candidate_id, 1), (peer_task.candidate_id, 1),
    ]
    snapshot = root / "replay" / "fork-points" / point.fork_point_id
    assert workspace_digest(snapshot / "workspace-files") == point.workspace_digest
    assert (snapshot / "repository.git").is_dir()
    assert not (snapshot / "run" / "reference-workspaces").exists()
    for current_task, row in zip(tasks, rows, strict=True):
        repository = snapshot / "candidate-repositories" / f"{current_task.candidate_id}.git"
        assert repository.is_dir()
        subprocess.run(
            ["git", "cat-file", "-e", f"{row['artifact_ref']['id']}^{{commit}}"],
            cwd=repository, check=True,
        )
    assert (snapshot / "goal").is_dir()
    if provider == "copy":
        assert (snapshot / "baseline-source" / "initial_program.py").is_file()
    rollout = tmp_path / "source-rollout.jsonl"
    rollout.write_text("\n".join(json.dumps(item) for item in [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-source"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "continue"}],
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "outer-source",
            "input": "text(await tools.goal_plus_search_get_global_evidence({agent_session_id: 'one'}))",
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "outer-source",
            "output": [
                {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
                {"type": "input_text", "text": json.dumps(rows)},
            ],
        }},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-source"}},
    ]), encoding="utf-8")
    monkeypatch.setattr(replay_native, "discover_codex_session_file", lambda _session: rollout)
    replay_native.finalize_turn(root, "thread-source", "turn-source", completed=True)
    ready = FileReplayCapture(root).read(point.fork_point_id)
    assert ready.state == "ready", ready.reason
    assert ready.native_outer_call_id == "outer-source"
    monkeypatch.setattr(replay_experiment, "_fork_native", lambda *_args: "thread-branch")

    class FakeCodexSession:
        def __init__(self, config, _registration, *, timeout, tool_bridge):
            self.workspace = config.process.cwd
            self.bridge = tool_bridge

        def run(self, prompt: str, *, timeout: float):
            assert f"Selected Evidence: {peer_task.candidate_id}:1" in prompt
            detail = self.bridge.call("goal_plus_search_get_evidence_detail", {
                "candidate_id": peer_task.candidate_id,
                "iteration": 1,
            })
            assert (detail["candidate_id"], detail["iteration"]) == (peer_task.candidate_id, 1)
            assert detail["supplemental_evaluation"]["summary"] == "The change is useful."
            opened = self.bridge.call("goal_plus_search_reference_open", {
                "source_candidate_id": peer_task.candidate_id,
                "source_iteration": 1,
            })
            assert Path(opened["access"]["root"]).joinpath(
                "initial_program.py",
            ).read_text(encoding="utf-8") == "VALUE = 2\n"
            (self.workspace / "initial_program.py").write_text("VALUE = 2\n", encoding="utf-8")
            return SessionInvocationResult(
                "thread-branch", "turn-branch", "completed", "done",
                agent_harness="codex", runtime_provider="direct",
            )

        def close(self):
            pass

    monkeypatch.setattr(replay_experiment, "CodexSession", FakeCodexSession)
    service = FileReplayExperiments(root)
    experiment = service.create(ready.fork_point_id, [(peer_task.candidate_id, 1)],
                                request_id=f"linked-{provider}")
    finished = service.run(experiment.experiment_id)
    assert finished.branches[0].state == "completed", finished.branches[0].error
    assert len(finished.branches[0].result["iterations"]) == 1
    assert (task.workspace / "initial_program.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    replay_goal = json.loads((
        service.base / experiment.experiment_id / finished.branches[0].branch_id
        / "runtime" / "goal-plus" / goal.goal_plus_id / "goal.json"
    ).read_text(encoding="utf-8"))
    remaining = (
        datetime.fromisoformat(replay_goal["outer_deadline_at"])
        - datetime.now(timezone.utc)
    ).total_seconds()
    verifier_window = sum(
        float(command.timeout_seconds) + 2 * replay_experiment.TERM_GRACE_SECONDS
        for command in frozen.spec.process_verifiers
    )
    assert remaining > ready.remaining_runtime_seconds + verifier_window - 10


def _point(root: Path) -> tuple[ReplayForkPoint, list[dict]]:
    rows = []
    targets = []
    directory = root / "replay" / "fork-points" / ("fork_" + "a" * 32)
    for index, name in enumerate(("A", "B", "C"), 1):
        commit = str(index) * 40
        artifact = ArtifactRef(kind="git_commit", id=commit, provider="git_worktree")
        detail = {
            "candidate_id": name, "iteration": index, "commit": commit,
            "supplemental_evaluation": {"finding": name},
        }
        digest = sha256_text(canonical_json(detail))
        write_json(directory / "details" / f"{digest}.json", detail)
        rows.append({
            "candidate_id": name, "iteration": index, "commit": commit,
            "artifact_ref": artifact.model_dump(mode="json"),
            "supplemental_available": True,
        })
        targets.append(ReplayTarget(
            candidate_id=name, iteration=index, artifact_ref=artifact,
            detail_snapshot_id=digest,
        ))
    prefix = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]}]
    point = ReplayForkPoint(
        fork_point_id=directory.name, state="ready", run_id="run_1",
        source_root=str(root), candidate_id="candidate_1", agent_session_id="session_1",
        execution_generation=0, frozen_spec_id="spec_1",
        source_iteration_count=0,
        native_thread_id="thread_1", native_turn_id="turn_1",
        native_tool_call_id="exec_1", native_outer_call_id="call_1",
        source="explicit", operation_id="read_1",
        global_snapshot_id=sha256_text(canonical_json(rows)),
        workspace_provider="git_worktree",
        workspace_head="a" * 40, workspace_digest="b" * 64,
        run_digest="c" * 64, created_at="2026-09-28T00:00:00Z",
        targets=targets, launch={"model": "test-model"},
        remaining_runtime_seconds=300,
        native_prefix_digest=sha256_text(canonical_json(prefix)),
    )
    write_json(directory / "manifest.json", point.model_dump(mode="json"))
    write_json(directory / "global-evidence.json", rows)
    write_json(directory / "prefix.json", prefix)
    return point, rows


def test_create_only_selected_evidence_and_idempotent_request(tmp_path: Path) -> None:
    point, _ = _point(tmp_path)
    service = FileReplayExperiments(tmp_path)
    only_b = service.create(point.fork_point_id, [("B", 2)], request_id="one")
    assert [(item.target.candidate_id, item.target.iteration) for item in only_b.branches] == [("B", 2)]
    assert service.create(point.fork_point_id, [("B", 2)], request_id="one") == only_b
    a_and_c = service.create(point.fork_point_id, [("A", 1), ("C", 3)], request_id="two")
    assert [item.target.candidate_id for item in a_and_c.branches] == ["A", "C"]


def test_create_rejects_ambiguous_or_changed_targets(tmp_path: Path) -> None:
    point, rows = _point(tmp_path)
    service = FileReplayExperiments(tmp_path)
    for targets in ([], [("B", 2), ("B", 2)], [("D", 1)], [("B", 1)]):
        with pytest.raises(ValueError):
            service.create(point.fork_point_id, targets, request_id=f"bad-{targets}")
    service.create(point.fork_point_id, [("B", 2)], request_id="same")
    with pytest.raises(ValueError, match="request_id"):
        service.create(point.fork_point_id, [("A", 1)], request_id="same")
    rows[1]["artifact_ref"]["id"] = "f" * 40
    write_json(tmp_path / "replay" / "fork-points" / point.fork_point_id / "global-evidence.json", rows)
    with pytest.raises(RuntimeError, match="GlobalEvidenceSnapshotMismatch"):
        service.create(point.fork_point_id, [("B", 2)], request_id="changed")


def test_concurrent_run_does_not_reclaim_active_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    point, _ = _point(tmp_path)
    service = FileReplayExperiments(tmp_path)
    experiment = service.create(point.fork_point_id, [("B", 2)], request_id="concurrent")
    started, release = Event(), Event()
    calls = []

    def execute(*_args):
        calls.append(1)
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(service, "_run_claimed_branch", execute)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(service.run, experiment.experiment_id)
        assert started.wait(5)
        second = service.run(experiment.experiment_id)
        assert len(calls) == 1
        assert second.branches[0].state == "created"
        release.set()
        first.result(timeout=5)


def test_interrupted_branch_requires_stop_confirmation_before_cleanup(tmp_path: Path) -> None:
    point, _ = _point(tmp_path)
    service = FileReplayExperiments(tmp_path)
    experiment = service.create(point.fork_point_id, [("B", 2)], request_id="interrupted")
    branch = experiment.branches[0].model_copy(update={
        "state": "failed", "error": "ReplayInterrupted: inspect retained branch runtime",
    })
    service._update_branch(experiment, branch)
    with pytest.raises(RuntimeError, match="RequiresManualProcessInspection"):
        service.cleanup(experiment.experiment_id)
    process_path = (
        service.base / experiment.experiment_id / branch.branch_id / "runtime"
        / "host-sessions" / "replay-processes" / "process.json"
    )
    write_json(process_path, {"pid": os.getpid(), "status": "running"})
    assert service.cleanup(experiment.experiment_id, confirmed_stopped=True).cleaned_at is not None


def test_native_prefix_stops_at_single_list_return(tmp_path: Path) -> None:
    point, rows = _point(tmp_path)
    user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "work"}]}
    call = {
        "type": "custom_tool_call", "name": "functions.exec", "call_id": "call_1",
        "input": "text(await tools.goal_plus_search_get_global_evidence({agent_session_id: 'session_1'}))",
    }
    output = {
        "type": "custom_tool_call_output", "call_id": "call_1",
        "output": [
            {"type": "input_text", "text": "Script completed\nWall time 1.0 seconds\nOutput:\n"},
            {"type": "input_text", "text": json.dumps(rows)},
        ],
    }
    later = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "later"}]}
    events = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn_1"}},
        *({"type": "response_item", "payload": item} for item in (user, call, output, later)),
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn_1"}},
    ]
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
    assert verified_turn_prefix(rollout, point) == ([user, call, output], "call_1")
    events.insert(-1, {"type": "response_item", "payload": output})
    rollout.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Ambiguous"):
        verified_turn_prefix(rollout, point)


def test_fork_excludes_original_turn_and_injects_raw_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    point, _ = _point(tmp_path)
    prefix = json.loads((
        tmp_path / "replay" / "fork-points" / point.fork_point_id / "prefix.json"
    ).read_text(encoding="utf-8"))
    requests = []
    scratch = tmp_path / ".tmp" / "replay"
    monkeypatch.setenv("GOAL_PLUS_TMPDIR", str(scratch))
    monkeypatch.setenv("GOAL_PLUS_PARENT_PRIVATE_STATE", "parent-only")

    class FakeRpc:
        def __init__(self, process, *_args):
            scratch_dir = process.environment.get("GOAL_PLUS_TMPDIR")
            parent_state = process.environment.get("GOAL_PLUS_PARENT_PRIVATE_STATE")
            assert scratch_dir == str(scratch)
            assert parent_state is None

        def request(self, request, *, timeout):
            requests.append(request)
            if request["method"] == "thread/fork":
                return {"result": {"thread": {"id": "thread-branch"}}}
            if request["method"] == "thread/read":
                return {"result": {"thread": {"id": "thread-branch"}}}
            return {"result": {}}

        def send(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(replay_experiment, "SessionRpcProcess", FakeRpc)
    monkeypatch.setattr(replay_experiment, "verify_injected_prefix", lambda *_args: None)
    monkeypatch.setattr(
        replay_experiment, "candidate_host_queries",
        lambda *_args: type("Queries", (), {"process_environment": lambda _self, _id: {}})(),
    )
    assert replay_experiment._fork_native(point, tmp_path, tmp_path, prefix) == "thread-branch"
    assert requests[1]["params"]["beforeTurnId"] == point.native_turn_id
    assert requests[1]["params"]["runtimeWorkspaceRoots"] == [str(tmp_path)]
    assert requests[2]["params"]["items"] == prefix

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    rollout = tmp_path / "codex-home" / "sessions" / "2026" / "09" / "28" / "rollout-thread-branch.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("\n".join(json.dumps(item) for item in [
        {"type": "session_meta", "payload": {"id": "thread-branch"}},
        *({"type": "response_item", "payload": item} for item in prefix),
    ]), encoding="utf-8")
    verify_injected_prefix("thread-branch", prefix)

    reasoning = {
        "type": "reasoning", "id": "reasoning_1", "summary": [],
        "encrypted_content": "opaque",
    }
    normalized_by_codex = {**reasoning, "content": None}
    rollout.write_text("\n".join(json.dumps(item) for item in [
        {"type": "session_meta", "payload": {"id": "thread-branch"}},
        {"type": "response_item", "payload": prefix[0]},
        {"type": "response_item", "payload": normalized_by_codex},
    ]), encoding="utf-8")
    verify_injected_prefix("thread-branch", [prefix[0], reasoning])

    with pytest.raises(RuntimeError, match="ReadbackMismatch"):
        verify_injected_prefix("thread-branch", [{"type": "message", "role": "wrong"}])


def test_branch_materialization_keeps_files_and_git_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, run_id, [(candidate_id, session_id, workspace)] = _search_with_candidates(
        tmp_path / "source", 1, metric_direction="minimize",
    )
    (workspace / ".tmp" / "untracked.txt").write_text("private edit", encoding="utf-8")
    captured_json = json.dumps({"workspace": str(workspace)}) + "\n"
    (workspace / ".tmp" / "context.json").write_text(captured_json, encoding="utf-8")
    source_run = runtime.root_dir / "runs" / run_id
    reference_json = source_run / "reference-workspaces" / "test" / "context.json"
    reference_json.parent.mkdir(parents=True, exist_ok=True)
    reference_json.write_text(captured_json, encoding="utf-8")
    artifact_json = (
        source_run / "evidence-annotator" / "artifact-views" / "test" / "context.json"
    )
    artifact_json.parent.mkdir(parents=True, exist_ok=True)
    artifact_json.write_text(captured_json, encoding="utf-8")
    candidate = runtime.queries.state.read_candidate(run_id, candidate_id)
    managed = candidate.task.workspace_managed_root or workspace
    point, _ = _point(runtime.root_dir)
    point = point.model_copy(update={
        "source_root": str(runtime.root_dir), "run_id": run_id,
        "candidate_id": candidate_id, "agent_session_id": session_id,
        "frozen_spec_id": runtime.queries.state.read_run(run_id).frozen_spec_id,
        "workspace_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=managed, text=True,
        ).strip(),
        "workspace_digest": workspace_digest(managed),
    })
    source = runtime.root_dir / "replay" / "fork-points" / point.fork_point_id
    shutil.copytree(source_run, source / "run", ignore=shutil.ignore_patterns(
        "workspace", "workspace-repository.git", "workspace-repository",
    ))
    shutil.copytree(runtime.root_dir / "specs" / point.frozen_spec_id, source / "spec")
    repository = source_run / "workspace-repository.git"
    if not repository.exists():
        repository = source_run / "workspace-repository"
    subprocess.run([
        "git", "clone", "--mirror", "--no-hardlinks",
        str(repository), str(source / "repository.git"),
    ], check=True, capture_output=True)
    shutil.copytree(managed, source / "workspace-files", ignore=shutil.ignore_patterns(".git"))
    write_json(source / "manifest.json", point.model_dump(mode="json"))
    service = FileReplayExperiments(runtime.root_dir)
    experiment = ReplayExperiment(
        experiment_id="replay_" + "a" * 32, fork_point_id=point.fork_point_id,
        request_id="materialize", created_at=point.created_at,
        branches=[ReplayBranch(branch_id="branch_" + "b" * 32, target=point.targets[0])],
    )
    branch_root, branch_workspace = service._materialize(point, experiment, experiment.branches[0])
    assert workspace_digest(branch_workspace) == point.workspace_digest
    assert (branch_workspace / ".tmp" / "untracked.txt").read_text(encoding="utf-8") == "private edit"
    assert (branch_workspace / ".tmp" / "context.json").read_text(encoding="utf-8") == captured_json
    assert not (
        branch_root / "runs" / run_id / "reference-workspaces" / "test" / "context.json"
    ).exists()
    assert (
        branch_root / "runs" / run_id / "evidence-annotator"
        / "artifact-views" / "test" / "context.json"
    ).read_text(encoding="utf-8") == captured_json
    (branch_workspace / ".tmp" / "untracked.txt").write_text("branch edit", encoding="utf-8")
    assert (workspace / ".tmp" / "untracked.txt").read_text(encoding="utf-8") == "private edit"
    assert str(branch_root) in (branch_workspace / ".git").read_text(encoding="utf-8")

    original_run = (source_run / "run.json").read_bytes()
    original_candidate = (source_run / "candidates" / candidate_id / "candidate.json").read_bytes()
    started = []

    class FakeCodexSession:
        def __init__(self, config, _registration, *, timeout, tool_bridge):
            started.append((config.process.cwd, timeout))
            self.workspace = config.process.cwd

        def run(self, prompt: str, *, timeout: float):
            assert "Selected Evidence: B:2" in prompt
            assert "Replay workspace:" in prompt
            (self.workspace / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")
            return SessionInvocationResult(
                "thread-branch", "turn-branch", "completed", "done",
                agent_harness="codex", runtime_provider="direct",
            )

        def close(self):
            pass

    monkeypatch.setattr(replay_experiment, "_fork_native", lambda *_args: "thread-branch")
    monkeypatch.setattr(replay_experiment, "McpSessionToolBridge", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(replay_experiment, "CodexSession", FakeCodexSession)
    only_b = service.create(point.fork_point_id, [("B", 2)], request_id="run-only-b")
    finished = service.run(only_b.experiment_id)
    assert len(started) == 1
    assert len(finished.branches) == 1
    assert finished.branches[0].state == "completed", finished.branches[0].error
    assert finished.branches[0].target.candidate_id == "B"
    assert len(finished.branches[0].result["iterations"]) == 1
    assert finished.branches[0].result["iterations"][0]["process_passed"] is True
    assert (source_run / "run.json").read_bytes() == original_run
    assert (source_run / "candidates" / candidate_id / "candidate.json").read_bytes() == original_candidate
    valid = finished.branches[0].result["iterations"][0]
    invalid = {
        **valid,
        "iteration": valid["iteration"] + 1,
        "process_passed": False,
        "score": 0.0,
    }
    amended_branch = finished.branches[0].model_copy(update={
        "result": {**finished.branches[0].result, "iterations": [invalid, valid]},
    })
    service._update_branch(finished, amended_branch)
    comparison = service.report(only_b.experiment_id)["comparison"][0]
    assert comparison["branch_id"] == finished.branches[0].branch_id
    assert comparison["best_iteration"] == valid["iteration"]
    assert comparison["best_score"] == valid["score"]
    cleaned = service.cleanup(only_b.experiment_id)
    assert cleaned.cleaned_at is not None
    assert service.cleanup(only_b.experiment_id) == cleaned
    assert not (service.base / only_b.experiment_id / finished.branches[0].branch_id / "runtime").exists()
    released = service.release(point.fork_point_id)
    assert released.state == "unavailable"
    assert not (source / "repository.git").exists()
    assert service.report(only_b.experiment_id)["source_list_snapshot_id"] == point.global_snapshot_id


def test_copy_provider_branch_restores_local_git_and_verifier(tmp_path: Path) -> None:
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    project = make_project(source_parent)
    runtime = FileSearchRuntime(source_parent / ".gp")
    run_id, candidate_id, workspace = create_candidate(runtime, project)
    session = runtime.start_agent_session(run_id, candidate_id)
    point, _ = _point(runtime.root_dir)
    point = point.model_copy(update={
        "source_root": str(runtime.root_dir), "run_id": run_id,
        "candidate_id": candidate_id, "agent_session_id": session.agent_session_id,
        "frozen_spec_id": runtime.queries.state.read_run(run_id).frozen_spec_id,
        "workspace_provider": "copy",
        "workspace_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=workspace, text=True,
        ).strip(),
        "workspace_digest": workspace_digest(workspace),
    })
    source_run = runtime.root_dir / "runs" / run_id
    source = runtime.root_dir / "replay" / "fork-points" / point.fork_point_id
    shutil.copytree(source_run, source / "run", ignore=shutil.ignore_patterns("workspace"))
    shutil.copytree(runtime.root_dir / "specs" / point.frozen_spec_id, source / "spec")
    subprocess.run([
        "git", "clone", "--mirror", "--no-hardlinks", str(workspace),
        str(source / "repository.git"),
    ], check=True, capture_output=True)
    shutil.copytree(workspace, source / "workspace-files", ignore=shutil.ignore_patterns(".git"))
    copy_source_tree(project, source / "baseline-source")
    write_json(source / "manifest.json", point.model_dump(mode="json"))
    experiment = ReplayExperiment(
        experiment_id="replay_" + "c" * 32, fork_point_id=point.fork_point_id,
        request_id="copy", created_at=point.created_at,
        branches=[ReplayBranch(branch_id="branch_" + "d" * 32, target=point.targets[0])],
    )
    branch_root, branch_workspace = FileReplayExperiments(runtime.root_dir)._materialize(
        point, experiment, experiment.branches[0],
    )
    assert workspace_digest(branch_workspace) == point.workspace_digest
    assert (branch_workspace / ".git").is_dir()
    (branch_workspace / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")
    report = FileSearchRuntime(branch_root).run_verifier(
        run_id, candidate_id, agent_session_id=session.agent_session_id, hypothesis="copy replay",
    )
    assert report.process_passed is True
    assert (workspace / "initial_program.py").read_text(encoding="utf-8") == "VALUE = 0\n"
