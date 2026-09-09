from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from goal_plus.ports.annotation import EvidenceAnnotationResult
from goal_plus.annotation_host import AnnotationHostService
from goal_plus.models import ArtifactRef, CandidateProposal, SearchSpec
from goal_plus.runtime import FileSearchRuntime
from goal_plus.thinkthread_agent_posix import (
    AGENT_POSIX_CONTRACT_FINGERPRINT,
    AGENT_POSIX_CONTROL_PROTOCOL_VERSION,
    AgentPosixBridgeError,
    AgentPosixSdkClient,
    validate_thinkthread_root,
)
from goal_plus.thinkthread_annotation import (
    annotation_snapshot,
    close_thinkthread_annotation,
    service_thinkthread_annotation,
)
from goal_plus.thinkthread_artifacts import (
    ThinkThreadArtifactViewRouter,
    decode_fs_run_output,
    fs_path_text,
)
from goal_plus.thinkthread_pool import (
    close_pool,
    continue_thinkthread_search_pool,
    open_pool,
    open_thinkthread_search_pool,
    wait_any,
)


def _request_id(label: str) -> str:
    return f"req-{uuid.uuid5(uuid.NAMESPACE_URL, f'goal-plus-st:{label}')}"


def _close_request(client: AgentPosixSdkClient, request_id: str) -> None:
    client.invoke("fs.request.close", {"requestId": request_id})


def _snapshot_create(client: AgentPosixSdkClient, label: str) -> str:
    request_id = _request_id(f"snapshot:{label}")
    result = client.invoke("fs.snapshot.create", {"requestId": request_id})
    assert result["snapshotId"].startswith("fsnap-")
    status = client.invoke("fs.request.status", {"requestId": request_id})
    assert status["state"] == "succeeded"
    assert status["result"] == result
    _close_request(client, request_id)
    return str(result["snapshotId"])


def _snapshot_patch(
    client: AgentPosixSdkClient,
    snapshot_id: str,
    label: str,
    mutations: list[dict[str, Any]],
) -> str:
    request_id = _request_id(f"patch:{label}")
    result = client.invoke(
        "workflow.fs.snapshot.patchBytes",
        {
            "snapshotId": snapshot_id,
            "mutations": mutations,
            "requestId": request_id,
        },
        timeout_seconds=120,
    )
    snapshot = result["snapshot"]
    assert snapshot["snapshotId"].startswith("fsnap-")
    replay = client.invoke(
        "workflow.fs.snapshot.patchBytes",
        {
            "snapshotId": snapshot_id,
            "mutations": mutations,
            "requestId": request_id,
        },
        timeout_seconds=120,
    )
    assert replay == result
    _close_request(client, request_id)
    return str(snapshot["snapshotId"])


def _run_snapshot(
    client: AgentPosixSdkClient,
    snapshot_id: str,
    label: str,
    *,
    writes: str,
    script: str,
) -> dict[str, Any]:
    request_id = _request_id(f"run:{label}")
    result = client.invoke(
        "fs.run",
        {
            "snapshotId": snapshot_id,
            "invocation": {"argv": ["/bin/sh", "-c", script], "cwd": "."},
            "writes": writes,
            "limits": {"timeoutMs": 30_000, "maxOutputBytes": 64 * 1024},
            "requestId": request_id,
        },
        timeout_seconds=60,
    )
    assert result["exit"] == {"kind": "code", "code": 0}
    assert result["runKey"]
    _close_request(client, request_id)
    return result


def _apply(
    client: AgentPosixSdkClient,
    base: str,
    target: str,
    label: str,
    *,
    dependencies: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], str]:
    request_id = _request_id(f"apply:{label}")
    params = {
        "baseSnapshotId": base,
        "targetSnapshotId": target,
        "dependencies": dependencies or [],
        "policyId": "safe_content_v1",
        "requestId": request_id,
    }
    result = client.invoke("fs.apply", params, timeout_seconds=120)
    replay = client.invoke("fs.apply", params, timeout_seconds=120)
    assert replay == result
    return result, request_id


def _remove_snapshot(client: AgentPosixSdkClient, snapshot_id: str, label: str) -> None:
    request_id = _request_id(f"remove:{label}:{snapshot_id}")
    client.invoke(
        "fs.snapshot.remove",
        {"snapshotId": snapshot_id, "requestId": request_id},
        timeout_seconds=120,
    )
    _close_request(client, request_id)


def _put(path: str, content: bytes, *, mode: int = 0o644) -> dict[str, Any]:
    return {
        "kind": "put_file",
        "path": path,
        "dataBase64": base64.b64encode(content).decode("ascii"),
        "mode": mode,
    }


def run_abi_smoke(config: dict[str, Any]) -> dict[str, Any]:
    client = AgentPosixSdkClient()
    root = validate_thinkthread_root(client)
    workspace = Path(config["report_path"]).parent
    runtime_root = workspace / ".gp-tt-smoke"
    workspace.joinpath("abi.txt").write_text("base\n", encoding="utf-8")
    workspace.joinpath("dependency.txt").write_text("dependency-v1\n", encoding="utf-8")
    workspace.joinpath("delete-me.txt").write_text("delete\n", encoding="utf-8")

    snapshots: list[str] = []
    s0 = _snapshot_create(client, "abi-s0")
    snapshots.append(s0)
    snapshot_stat = client.invoke("fs.snapshot.stat", {"snapshotId": s0})
    file_stat = client.invoke(
        "fs.snapshot.stat", {"snapshotId": s0, "path": "abi.txt"}
    )
    assert snapshot_stat["ownerThinkthreadId"] == root["thinkthreadId"]
    assert file_stat["kind"] == "file"
    assert client.snapshot_read_file(s0, "abi.txt") == b"base\n"
    entries = client.snapshot_readdir_all(s0, ".", page_limit=1)
    assert {fs_path_text(entry["path"]) for entry in entries} >= {
        "abi.txt",
        "dependency.txt",
        "delete-me.txt",
    }

    publication = _snapshot_patch(
        client,
        s0,
        "abi-publication",
        [
            {"kind": "make_directory", "path": "created", "mode": 0o755},
            _put("created/new.txt", b"new\n", mode=0o600),
            _put("abi.txt", b"candidate\n"),
            {"kind": "remove_file", "path": "delete-me.txt"},
        ],
    )
    snapshots.append(publication)
    changes = client.snapshot_diff_all(s0, publication, page_limit=1)
    assert {fs_path_text(change["path"]) for change in changes} >= {
        "abi.txt",
        "created",
        "created/new.txt",
        "delete-me.txt",
    }

    deny_run = _run_snapshot(
        client,
        publication,
        "deny",
        writes="deny",
        script="test \"$(cat abi.txt)\" = candidate",
    )
    discard_run = _run_snapshot(
        client,
        publication,
        "discard",
        writes="discard",
        script="printf transient > discarded.txt; test -f discarded.txt",
    )
    snapshot_run = _run_snapshot(
        client,
        publication,
        "snapshot",
        writes="snapshot",
        script="printf retained > retained.txt",
    )
    retained = str(snapshot_run["targetSnapshotId"])
    snapshots.append(retained)
    assert client.snapshot_read_file(retained, "retained.txt") == b"retained"

    workspace.joinpath("unrelated.txt").write_text("main-unrelated\n", encoding="utf-8")
    applied, apply_request = _apply(client, s0, publication, "unrelated")
    assert applied["status"] == "applied"
    assert workspace.joinpath("abi.txt").read_text(encoding="utf-8") == "candidate\n"
    assert workspace.joinpath("unrelated.txt").read_text(encoding="utf-8") == (
        "main-unrelated\n"
    )
    assert workspace.joinpath("created/new.txt").read_text(encoding="utf-8") == "new\n"
    assert not workspace.joinpath("delete-me.txt").exists()
    apply_status = client.invoke("fs.request.status", {"requestId": apply_request})
    assert apply_status["state"] == "succeeded"
    _close_request(client, apply_request)

    conflict_base = _snapshot_create(client, "conflict-base")
    snapshots.append(conflict_base)
    conflict_target = _snapshot_patch(
        client,
        conflict_base,
        "conflict-target",
        [_put("abi.txt", b"candidate-conflict\n"), _put("must-be-atomic.txt", b"new\n")],
    )
    snapshots.append(conflict_target)
    workspace.joinpath("abi.txt").write_text("main-conflict\n", encoding="utf-8")
    conflict_request = _request_id("apply:changed-conflict")
    try:
        client.invoke(
            "fs.apply",
            {
                "baseSnapshotId": conflict_base,
                "targetSnapshotId": conflict_target,
                "dependencies": [],
                "policyId": "safe_content_v1",
                "requestId": conflict_request,
            },
            timeout_seconds=120,
        )
    except AgentPosixBridgeError as exc:
        assert exc.code == "FsApplyConflict"
    else:
        raise AssertionError("changed-path apply unexpectedly succeeded")
    assert workspace.joinpath("abi.txt").read_text(encoding="utf-8") == "main-conflict\n"
    assert not workspace.joinpath("must-be-atomic.txt").exists()
    _close_request(client, conflict_request)

    dependency_base = _snapshot_create(client, "dependency-base")
    snapshots.append(dependency_base)
    dependency_target = _snapshot_patch(
        client,
        dependency_base,
        "dependency-target",
        [_put("dependency-target.txt", b"candidate\n")],
    )
    snapshots.append(dependency_target)
    workspace.joinpath("dependency.txt").write_text("dependency-v2\n", encoding="utf-8")
    dependency_request = _request_id("apply:dependency-conflict")
    try:
        client.invoke(
            "fs.apply",
            {
                "baseSnapshotId": dependency_base,
                "targetSnapshotId": dependency_target,
                "dependencies": [{"path": "dependency.txt", "scope": "content"}],
                "policyId": "safe_content_v1",
                "requestId": dependency_request,
            },
            timeout_seconds=120,
        )
    except AgentPosixBridgeError as exc:
        assert exc.code == "FsApplyConflict"
    else:
        raise AssertionError("dependency apply unexpectedly succeeded")
    assert not workspace.joinpath("dependency-target.txt").exists()
    _close_request(client, dependency_request)

    smoke = open_pool(
        root_dir=runtime_root,
        run_id="st_single_child_smoke",
        candidates=[
            {
                "candidate_id": "smoke",
                "message": (
                    "This is TT-LNX-CHILD-001 single Child smoke. Do not modify files or call "
                    "tools. Reply exactly THINKTHREAD_CHILD_SMOKE_READY and end the turn."
                ),
                "max_runtime_seconds": 180,
            }
        ],
        max_parallel=1,
        baseline_snapshot_id=dependency_base,
        client=client,
    )
    event = wait_any(
        root_dir=runtime_root,
        pool_id=smoke["pool_id"],
        timeout_seconds=180,
        client=client,
    )["event"]
    assert event is not None and event["kind"] == "candidate_ready"
    smoke_job = event["result"]
    assert smoke_job["thinkthread_id"].startswith("tt-")
    smoke_branch_id = str(smoke["jobs"][0]["fs_branch_id"])
    closed = close_pool(
        root_dir=runtime_root,
        pool_id=smoke["pool_id"],
        mode="drain",
        client=client,
    )
    assert closed["state"] == "closed"

    for index, snapshot_id in enumerate(reversed(snapshots)):
        _remove_snapshot(client, snapshot_id, f"abi-{index}")
    final = client.invoke("thinkthread.list").get("children")
    assert final == []
    return {
        "schema_version": 1,
        "scenario": config["scenario"],
        "passed_test_ids": [
            "TT-LNX-CHILD-001",
            "TT-LNX-APPLY-001",
            "TT-LNX-CONFLICT-001",
            "TT-LNX-CONFLICT-002",
        ],
        "root_thinkthread_id": root["thinkthreadId"],
        "single_child": {
            "thinkthread_id": smoke_job["thinkthread_id"],
            "fs_branch_id": smoke_branch_id,
        },
        "snapshots": {
            "baseline": s0,
            "publication": publication,
            "run_snapshot": retained,
        },
        "request_ids": {
            "apply": apply_request,
            "changed_conflict": conflict_request,
            "dependency_conflict": dependency_request,
        },
        "abi": {
            "diff_paths": sorted(fs_path_text(change["path"]) for change in changes),
            "deny_run_key": deny_run["runKey"],
            "discard_run_key": discard_run["runKey"],
            "snapshot_run_key": snapshot_run["runKey"],
        },
    }


def _runtime_spec(
    project: Path,
    *,
    annotation_host: str = "codex",
    inner_agent: str | None = None,
) -> SearchSpec:
    payload = {
        "objective": (
            "Create one isolated implementation by replacing the integer in "
            "solution.txt exactly as assigned, then request the process verifier."
        ),
        "metric_name": "combined_score",
        "metric_direction": "maximize",
        "source_path": str(project),
        "edit_surface": {
            "allow": ["solution.txt"],
            "deny": ["evaluator.py", "dependency.txt"],
        },
        "budget": {"max_parallel": 2},
        "process_verifiers": [
            {
                "name": "exact_snapshot_score",
                "role": "ranking_signal",
                "command": ["python", "evaluator.py"],
                "timeout_seconds": 30,
            }
        ],
        "strategy": {
            "name": "agent_guided",
            "worker_budget": {"max_runtime_seconds": 240},
            "worker_launch": {},
            "evidence_annotator": {},
        },
        "workspace": {
            "backend": "thinkthread",
            "promotion_mode": "apply",
        },
    }
    if inner_agent is not None:
        payload["strategy"]["inner_agent"] = inner_agent
    return SearchSpec.model_validate(payload)


def _runtime_project(workspace: Path) -> Path:
    project = workspace / "project"
    project.mkdir()
    project.joinpath("solution.txt").write_text("0\n", encoding="utf-8")
    project.joinpath("dependency.txt").write_text("stable\n", encoding="utf-8")
    project.joinpath("evaluator.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "score = float(Path('solution.txt').read_text().strip())\n"
        "print(json.dumps({'combined_score': score}))\n",
        encoding="utf-8",
    )
    return project


def _annotation_spec(project: Path) -> SearchSpec:
    payload = _runtime_spec(project, annotation_host="thinkthread").model_dump(
        mode="json"
    )
    payload["objective"] = (
        "Create one isolated implementation by replacing solution.txt and the first "
        "byte of large.txt exactly as assigned, then run the process verifier. For "
        "an admitted Evidence annotation task that has a peer, inspect the immutable "
        "Artifact Views before submitting: list the target project directory; stat "
        "project/large.txt; pread project/large.txt from both target and peer; diff "
        "peer against target; and run /bin/sh -c 'printf transient > "
        "annotation-write.txt; python evaluator.py' with cwd project on the target. "
        "Submit one concise structured annotation only after all those operations."
    )
    payload["edit_surface"]["allow"] = ["solution.txt", "large.txt"]
    return SearchSpec.model_validate(payload)


def _annotation_project(workspace: Path) -> Path:
    project = _runtime_project(workspace)
    project.joinpath("large.txt").write_bytes(b"0" + (b"x" * (1024 * 1024 + 256)))
    project.joinpath("evaluator.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "score = float(Path('solution.txt').read_text().strip())\n"
        "large = Path('large.txt').read_bytes()\n"
        "assert large[:1] == str(int(score)).encode()\n"
        "print(json.dumps({'combined_score': score}))\n",
        encoding="utf-8",
    )
    return project


def _annotation_proposal(value: int) -> CandidateProposal:
    return CandidateProposal(
        intent=f"Create isolated large-diff candidate {value}.",
        hypothesis=f"Candidate {value} proves exact large snapshot Evidence.",
        instructions=[
            f"Set project/solution.txt to exactly `{value}` followed by a newline.",
            (
                "Use a local command that does not print the file to replace only the "
                f"first byte of project/large.txt with `{value}`; preserve every other byte."
            ),
            "Call search_run_verifier exactly once with a concise hypothesis, then end the turn.",
        ],
    )


def _wait_for_annotations(
    runtime: FileSearchRuntime,
    run_id: str,
    client: AgentPosixSdkClient,
    *,
    timeout_seconds: float = 300,
) -> dict[str, Any]:
    timeout_seconds = float(
        os.environ.get("ST_THINKTHREAD_ANNOTATION_TIMEOUT", timeout_seconds)
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = service_thinkthread_annotation(
            root_dir=runtime.root_dir,
            run_id=run_id,
            client=client,
        )
        tasks = [
            runtime.annotation.task(
                run_id,
                record.candidate_id,
                1,
            )
            for record in runtime.candidate_creation.state.read_candidates(run_id)
        ]
        if (
            state is not None
            and state.get("status") == "ready"
            and tasks
            and all(task is not None and task.state == "completed" for task in tasks)
        ):
            return state
        if state is not None and state.get("status") in {"failed", "needs_recovery"}:
            raise RuntimeError(f"ThinkThread annotator failed: {state}")
        if any(
            task is not None and task.state == "terminal_error"
            for task in tasks
        ):
            raise RuntimeError(f"ThinkThread annotation task failed: {tasks}")
        time.sleep(0.2)
    raise TimeoutError(
        f"ThinkThread annotations did not settle: "
        f"{annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)}"
    )


def run_runtime_k2(config: dict[str, Any]) -> dict[str, Any]:
    client = AgentPosixSdkClient()
    validate_thinkthread_root(client)
    workspace = Path(config["report_path"]).parent
    project = _runtime_project(workspace)
    runtime_root = workspace / ".gp-tt-runtime-k2"
    runtime = FileSearchRuntime(runtime_root)
    inner_agent = (
        "autoresearch"
        if config["scenario"] == "runtime_k2_autoresearch"
        else "common"
    )
    frozen = runtime.freeze_spec(
        _runtime_spec(project, inner_agent=inner_agent),
        [project / "evaluator.py"], native_host="pi",
    )
    assert frozen.spec.strategy.inner_agent == inner_agent
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    inner_agent_binding = plan.worker_policy["inner_agent_binding"]
    assert plan.worker_policy["inner_agent"] == inner_agent
    assert inner_agent_binding["inner_agent_id"] == inner_agent
    assert inner_agent_binding["worker_host"] == "thinkthread"
    expected_projection = (
        "results_tsv" if inner_agent == "autoresearch" else "none"
    )
    assert inner_agent_binding["workspace_ledger_projection"] == expected_projection
    if inner_agent == "autoresearch":
        assert inner_agent_binding["prompt_asset"] == (
            "assets/pi/prompts/search-candidate-worker.md"
        )
    first_proposal = (
        CandidateProposal(
            intent="Improve the isolated candidate across retained wakes.",
            hypothesis="A sequence from one to three proves iterative continuation.",
            instructions=[
                "Read project/solution.txt. If its complete content is `0`, replace it with `1` followed by a newline. If its complete content is `1`, replace it with `3` followed by a newline.",
                "On every dispatch, call search_run_verifier exactly once with a concise hypothesis, then end the turn after the accepted receipt.",
            ],
        )
        if inner_agent == "autoresearch"
        else CandidateProposal(
            intent="Set the isolated candidate score to one.",
            hypothesis="A score of one proves the first private branch.",
            instructions=[
                "Edit only project/solution.txt so its complete content is exactly `1` followed by a newline.",
                "Call search_run_verifier exactly once with a concise hypothesis, then end the turn.",
            ],
        )
    )
    tasks = runtime.start_batch(
        run_id,
        plan.plan_id,
        proposals=[
            first_proposal,
            CandidateProposal(
                intent="Set the isolated candidate score to two.",
                hypothesis="A score of two proves the second private branch.",
                instructions=[
                    "Edit only project/solution.txt so its complete content is exactly `2` followed by a newline.",
                    "Call search_run_verifier exactly once with a concise hypothesis, then end the turn.",
                ],
            ),
        ],
    )
    opened = open_thinkthread_search_pool(
        root_dir=runtime_root,
        run_id=run_id,
        client=client,
    )
    pool_id = str(opened["pool_id"])
    events: list[dict[str, Any]] = []
    inner_git_verified = False
    artifact_view_private_paths_filtered = False
    scenario_completed = False
    try:
        for _ in tasks:
            waited = wait_any(
                root_dir=runtime_root,
                pool_id=pool_id,
                timeout_seconds=240,
                client=client,
            )
            event = waited["event"]
            assert event is not None, waited["pool"]
            assert event["kind"] == "candidate_ready", event
            pool_job = next(
                job
                for job in waited["pool"]["jobs"]
                if job["candidate_id"] == event["candidate_id"]
            )
            accepted = pool_job["attempt_request"]
            feedback = event["attempt_result"]
            assert accepted["state"] == "accepted"
            assert feedback["state"] == "verified"
            assert accepted["attempt_id"] == feedback["attempt_id"]
            assert accepted["request_id"] == feedback["request_id"]
            assert feedback == pool_job["attempt_result"]
            events.append(event)

        records = {
            record.candidate_id: record
            for record in runtime.candidate_creation.state.read_candidates(run_id)
        }
        events_by_candidate = {
            str(event["candidate_id"]): event for event in events
        }
        jobs = {str(job["candidate_id"]): job for job in opened["jobs"]}
        candidates: dict[str, Any] = {}
        for candidate_id, expected_score in (("c001", 1.0), ("c002", 2.0)):
            record = records[candidate_id]
            assert len(record.iterations) == 1
            iteration = record.iterations[0]
            worker_policy = record.task.strategy_metadata["worker_policy"]
            assert worker_policy["inner_agent"] == inner_agent
            assert worker_policy["inner_agent_binding"] == inner_agent_binding
            assert iteration.process_passed is True
            assert iteration.score == expected_score
            assert iteration.artifact_ref is not None
            assert iteration.artifact_ref.kind == "fs_snapshot"
            verifier_result = record.score_report.verifier_results[0]
            assert verifier_result.metrics["snapshot_id"] == iteration.artifact_ref.id
            snapshot_value = client.snapshot_read_file(
                iteration.artifact_ref.id,
                "project/solution.txt",
            )
            assert snapshot_value == f"{int(expected_score)}\n".encode()
            snapshot_entries = client.snapshot_readdir_all(
                iteration.artifact_ref.id,
                "project",
            )
            snapshot_paths = {
                fs_path_text(entry["path"]) for entry in snapshot_entries
            }
            if inner_agent == "autoresearch":
                assert any(Path(path).name == ".git" for path in snapshot_paths)
                assert any(Path(path).name == "results.tsv" for path in snapshot_paths)
                attempt_results = client.snapshot_read_file(
                    iteration.artifact_ref.id,
                    "project/results.tsv",
                ).decode("utf-8")
                assert len(attempt_results.splitlines()) == 1
                workspace_artifact = events_by_candidate[candidate_id][
                    "attempt_result"
                ]["workspace_artifact_after_attempt"]
                settled_results = client.snapshot_read_file(
                    workspace_artifact["id"],
                    "project/results.tsv",
                ).decode("utf-8")
                assert len(settled_results.splitlines()) == 2
                assert record.inner_agent_state["result_count"] == 1
                attempt_head = record.inner_agent_state["attempt_git_heads"]["1"]
                ledger_head = record.inner_agent_state["ledger_git_head"]
                _run_snapshot(
                    client,
                    workspace_artifact["id"],
                    f"inner-git:{run_id}:{candidate_id}:1",
                    writes="discard",
                    script=(
                        "set -eu; "
                        "test -d project/.git; "
                        f'test "$(git -C project rev-parse HEAD)" = "{ledger_head}"; '
                        f"git -C project merge-base --is-ancestor {attempt_head} {ledger_head}; "
                        'test "$(wc -l < project/results.tsv)" -eq 2'
                    ),
                )
                router = ThinkThreadArtifactViewRouter(
                    client,
                    {
                        "attempt": ArtifactRef(
                            kind="fs_snapshot",
                            id=iteration.artifact_ref.id,
                            provider="thinkthread",
                        )
                    },
                )
                visible = {
                    Path(fs_path_text(entry["path"])).name
                    for entry in router.readdir("attempt", "project")
                }
                assert ".git" not in visible
                assert "results.tsv" not in visible
                try:
                    router.stat("attempt", "project/.git/HEAD")
                except PermissionError:
                    pass
                else:
                    raise AssertionError("Artifact View exposed private Git metadata")
                artifact_view_request_id = _request_id(
                    f"artifact-view-private:{run_id}:{candidate_id}:1"
                )
                sanitized = router.run(
                    "attempt",
                    [
                        "/bin/sh",
                        "-c",
                        (
                            "pwd; remaining=0; "
                            "for path in .git results.tsv; do "
                            "if test -e \"$path\"; then "
                            "printf 'private-path-visible:%s\\n' \"$path\"; "
                            "remaining=1; fi; done; exit \"$remaining\""
                        ),
                    ],
                    cwd="project",
                    request_id=artifact_view_request_id,
                )
                assert sanitized["exit"] == {"kind": "code", "code": 0}, {
                    "result": sanitized,
                    "output": decode_fs_run_output(sanitized),
                }
                inner_git_verified = True
                artifact_view_private_paths_filtered = True
            else:
                assert not any(Path(path).name == ".git" for path in snapshot_paths)
                assert not any(
                    Path(path).name == "results.tsv" for path in snapshot_paths
                )
            if any(Path(path).name == ".tmp" for path in snapshot_paths):
                legacy_entries = client.snapshot_readdir_all(
                    iteration.artifact_ref.id,
                    "project/.tmp",
                )
                assert not any(
                    Path(fs_path_text(entry["path"])).name == "results.tsv"
                    for entry in legacy_entries
                )
            context = runtime.get_agent_context(iteration.agent_session_id)
            assert context["inner_agent"] == inner_agent
            assert context["result_ledger_source"] == "gp"
            assert context["workspace_ledger_projection"] == expected_projection
            assert context["results_tsv"] == (
                "results.tsv" if inner_agent == "autoresearch" else None
            )
            assert len(context["recent_iterations"]) == 1
            assert context["recent_iterations"][0]["attempt_id"] == (
                events_by_candidate[candidate_id]["attempt_result"]["attempt_id"]
            )
            assert len(record.results_ledger) == 1
            assert record.results_ledger_git_head is None
            assert not record.task.workspace.exists()
            session = runtime.candidate_sessions.session(iteration.agent_session_id)
            has_autoresearch_prompt = (
                "# Search Candidate Worker" in session.launch["message"]
            )
            assert has_autoresearch_prompt is (inner_agent == "autoresearch")
            candidates[candidate_id] = {
                "thinkthread_id": jobs[candidate_id]["thinkthread_id"],
                "agent_session_id": record.iterations[0].agent_session_id,
                "fs_branch_id": jobs[candidate_id]["fs_branch_id"],
                "artifact_ref": iteration.artifact_ref.model_dump(mode="json"),
                "verifier_snapshot_id": verifier_result.metrics["snapshot_id"],
                "score": iteration.score,
            }

        first_feedback = next(
            event["attempt_result"]
            for event in events
            if event["candidate_id"] == "c001"
        )
        continued = continue_thinkthread_search_pool(
            root_dir=runtime_root,
            pool_id=pool_id,
            candidate_id="c001",
            client=client,
        )
        continued_job = next(
            job for job in continued["jobs"] if job["candidate_id"] == "c001"
        )
        assert continued_job["dispatch_index"] == 2
        assert len(continued_job["attempt_history"]) == 1
        assert (
            continued_job["attempt_history"][0]["attempt_id"]
            == first_feedback["attempt_id"]
        )
        continued_wait = wait_any(
            root_dir=runtime_root,
            pool_id=pool_id,
            timeout_seconds=240,
            client=client,
        )
        continued_event = continued_wait["event"]
        assert continued_event is not None, continued_wait["pool"]
        assert continued_event["kind"] == "candidate_ready", continued_event
        assert continued_event["candidate_id"] == "c001"
        continued_pool_job = next(
            job
            for job in continued_wait["pool"]["jobs"]
            if job["candidate_id"] == "c001"
        )
        second_accepted = continued_pool_job["attempt_request"]
        second_feedback = continued_event["attempt_result"]
        assert second_accepted["attempt_id"] == second_feedback["attempt_id"]
        assert second_feedback["attempt_id"] != first_feedback["attempt_id"]
        assert second_feedback["attempt_iteration"] == 2
        continued_record = runtime.candidate_creation.state.read_candidate(run_id, "c001")
        assert len(continued_record.iterations) == 2
        assert len(continued_record.results_ledger) == 2
        assert continued_record.results_ledger_git_head is None
        if inner_agent == "autoresearch":
            assert continued_record.iterations[-1].score == 3.0
            assert continued_record.iterations[-1].disposition == "keep"
            assert continued_record.iterations[-1].artifact_ref is not None
            assert continued_record.iterations[-1].artifact_ref.kind == "fs_snapshot"
        continued_context = runtime.get_agent_context(
            str(candidates["c001"]["agent_session_id"])
        )
        assert continued_context["inner_agent"] == inner_agent
        assert continued_context["workspace_ledger_projection"] == expected_projection
        assert continued_context["results_tsv"] == (
            "results.tsv" if inner_agent == "autoresearch" else None
        )
        assert continued_context["recent_iterations"][-1]["attempt_id"] == (
            second_feedback["attempt_id"]
        )
        if inner_agent == "autoresearch":
            settled_snapshot_id = second_feedback[
                "workspace_artifact_after_attempt"
            ]["id"]
            results_lines = client.snapshot_read_file(
                settled_snapshot_id,
                "project/results.tsv",
            ).decode("utf-8").splitlines()
            assert len(results_lines) == 3
            assert continued_record.inner_agent_state["result_count"] == 2
            assert set(continued_record.inner_agent_state["attempt_git_heads"]) == {
                "1",
                "2",
            }
            first_head = continued_record.inner_agent_state["attempt_git_heads"]["1"]
            second_head = continued_record.inner_agent_state["attempt_git_heads"]["2"]
            ledger_head = continued_record.inner_agent_state["ledger_git_head"]
            _run_snapshot(
                client,
                settled_snapshot_id,
                f"inner-git:{run_id}:c001:2",
                writes="discard",
                script=(
                    "set -eu; "
                    f'test "$(git -C project rev-parse HEAD)" = "{ledger_head}"; '
                    f"git -C project merge-base --is-ancestor {first_head} {ledger_head}; "
                    f"git -C project merge-base --is-ancestor {second_head} {ledger_head}; "
                    'test "$(wc -l < project/results.tsv)" -eq 3'
                ),
            )

        assert project.joinpath("solution.txt").read_text(encoding="utf-8") == "0\n"
        assert len({item["thinkthread_id"] for item in candidates.values()}) == 2
        assert len({item["agent_session_id"] for item in candidates.values()}) == 2
        assert len({item["fs_branch_id"] for item in candidates.values()}) == 2
        assert len({item["artifact_ref"]["id"] for item in candidates.values()}) == 2
        scenario_completed = True
    finally:
        if (
            scenario_completed
            or os.environ.get("ST_THINKTHREAD_PRESERVE_ON_FAILURE") != "1"
        ):
            closed = close_pool(
                root_dir=runtime_root,
                pool_id=pool_id,
                mode="interrupt",
                client=client,
            )
    assert closed["state"] == "closed"
    assert client.invoke("thinkthread.list").get("children") == []
    return {
        "schema_version": 1,
        "scenario": config["scenario"],
        "passed_test_ids": [
            "TT-LNX-CHILD-001",
            "TT-LNX-VERIFY-001",
            *(
                ["TT-LNX-AUTORESEARCH-001"]
                if inner_agent == "autoresearch"
                else []
            ),
        ],
        "run_id": run_id,
        "baseline_snapshot_id": runtime.search_run.state.read_run(run_id).source_artifact_ref.id,
        "inner_agent": frozen.spec.strategy.inner_agent,
        "inner_agent_binding": inner_agent_binding,
        "autoresearch_prompt_loaded": inner_agent == "autoresearch",
        "inner_git_verified": inner_git_verified,
        "artifact_view_private_paths_filtered": (
            artifact_view_private_paths_filtered
        ),
        "event_candidates": sorted(event["candidate_id"] for event in events),
        "candidates": candidates,
        "continuation": {
            "candidate_id": "c001",
            "previous_attempt_id": first_feedback["attempt_id"],
            "history_attempt_id": continued_job["attempt_history"][0]["attempt_id"],
            "next_attempt_id": second_feedback["attempt_id"],
            "iteration_count": len(continued_record.iterations),
            "results_ledger_count": len(continued_record.results_ledger),
            "results_tsv": continued_context["results_tsv"],
            "score": continued_record.iterations[-1].score,
            "disposition": continued_record.iterations[-1].disposition,
        },
        "root_solution": project.joinpath("solution.txt").read_text(encoding="utf-8"),
        "cleanup_states": sorted(item["state"] for item in closed["cleanup"]),
    }


def run_annotation_views(config: dict[str, Any]) -> dict[str, Any]:
    os.environ["GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED"] = "1"
    client = AgentPosixSdkClient()
    validate_thinkthread_root(client)
    workspace = Path(config["report_path"]).parent
    project = _annotation_project(workspace)
    runtime_root = workspace / ".gp-tt-annotation"
    runtime = FileSearchRuntime(runtime_root)
    frozen = runtime.freeze_spec(_annotation_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    runtime.start_batch(
        run_id,
        plan.plan_id,
        proposals=[_annotation_proposal(1), _annotation_proposal(2)],
    )
    opened = open_thinkthread_search_pool(
        root_dir=runtime_root,
        run_id=run_id,
        client=client,
    )
    pool_id = str(opened["pool_id"])
    try:
        for _ in range(2):
            waited = wait_any(
                root_dir=runtime_root,
                pool_id=pool_id,
                timeout_seconds=300,
                client=client,
            )
            assert waited["event"] is not None, waited["pool"]
            assert waited["event"]["kind"] == "candidate_ready", waited["event"]

        state = _wait_for_annotations(runtime, run_id, client)
        settled = [
            record
            for record in state["settled_requests"].values()
            if isinstance(record, dict)
        ]
        tools = [str(record["tool"]) for record in settled]
        submit_responses = [
            record["response"]
            for record in settled
            if record.get("tool") == "thinkthread_annotation_submit"
            and isinstance(record.get("response"), dict)
        ]
        published_submit_count = sum(
            response.get("ok") is True
            and response.get("result", {}).get("published") is True
            for response in submit_responses
        )
        rejected_submit_count = sum(
            response.get("ok") is False for response in submit_responses
        )
        expected_tools = {
            "thinkthread_annotation_next",
            "thinkthread_artifact_view_list",
            "thinkthread_artifact_view_stat",
            "thinkthread_artifact_view_pread",
            "thinkthread_artifact_view_diff",
            "thinkthread_annotation_submit",
        }
        assert set(tools) == expected_tools
        assert tools.count("thinkthread_artifact_view_pread") >= 2

        claimed_contexts = [
            record["response"]["result"]["task"]
            for record in settled
            if record.get("tool") == "thinkthread_annotation_next"
            and record.get("response", {}).get("ok") is True
            and record["response"].get("result", {}).get("state") == "claimed"
        ]
        assert len(claimed_contexts) == 2
        assert any(context["actual_diff_omitted"] for context in claimed_contexts)
        for context in claimed_contexts:
            if context["actual_diff_omitted"]:
                assert context["actual_diff"] == ""
            if context["candidate_diff_omitted"]:
                assert context["candidate_diff"] == ""
            for peer in context["peer_evidence"]:
                if peer["diff_omitted"]:
                    assert peer["candidate_diff"] is None
        peer_context = next(
            context for context in claimed_contexts if context["comparison_basis"]
        )
        assert len(peer_context["artifact_views"]) == 2
        assert [view["role"] for view in peer_context["artifact_views"]] == [
            "target",
            "peer",
        ]

        records = runtime.candidate_creation.state.read_candidates(run_id)
        tasks = [
            runtime.annotation.task(run_id, record.candidate_id, 1)
            for record in records
        ]
        assert all(task is not None and task.view is not None for task in tasks)
        assert any(task.comparison_basis for task in tasks if task is not None)
        for record in records:
            snapshot_id = record.iterations[0].artifact_ref.id
            entries = client.snapshot_readdir_all(snapshot_id, "project")
            assert "annotation-write.txt" not in {
                fs_path_text(entry["path"]) for entry in entries
            }
    finally:
        closed = close_pool(
            root_dir=runtime_root,
            pool_id=pool_id,
            mode="interrupt",
            client=client,
        )
        if runtime.search_run.state.read_run(run_id).state not in {"ready_to_promote", "promoted"}:
            runtime.select(run_id)

    assert closed["state"] == "closed"
    assert annotation_snapshot(root_dir=runtime_root, run_id=run_id)["status"] == "closed"
    assert client.invoke("thinkthread.list").get("children") == []
    return {
        "schema_version": 1,
        "scenario": config["scenario"],
        "passed_test_ids": ["TT-LNX-ANNOTATE-001"],
        "run_id": run_id,
        "annotation_child_id": state["thinkthread_id"],
        "tool_counts": {tool: tools.count(tool) for tool in sorted(set(tools))},
        "published_submit_count": published_submit_count,
        "rejected_submit_count": rejected_submit_count,
        "actual_diff_omitted": [
            context["actual_diff_omitted"] for context in claimed_contexts
        ],
        "peer_comparison_count": len(peer_context["comparison_basis"]),
        "cleanup_states": sorted(item["state"] for item in closed["cleanup"]),
    }


def run_annotation_cancel(config: dict[str, Any]) -> dict[str, Any]:
    client = AgentPosixSdkClient()
    validate_thinkthread_root(client)
    workspace = Path(config["report_path"]).parent
    project = _runtime_project(workspace)
    runtime_root = workspace / ".gp-tt-annotation-cancel"
    runtime = FileSearchRuntime(runtime_root)
    frozen = runtime.freeze_spec(
        _runtime_spec(project, annotation_host="thinkthread"),
        [project / "evaluator.py"], native_host="pi",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    [candidate] = runtime.start_batch(
        run_id,
        plan.plan_id,
        proposals=[
            CandidateProposal(
                intent="Create one cancellable annotation Evidence task.",
                hypothesis="The exact private snapshot scores one.",
                instructions=[
                    "Set project/solution.txt to exactly `1` followed by a newline.",
                    "Call search_run_verifier exactly once, then end the turn.",
                ],
            )
        ],
    )
    opened = open_thinkthread_search_pool(
        root_dir=runtime_root,
        run_id=run_id,
        client=client,
    )
    pool_id = str(opened["pool_id"])
    selected = False
    try:
        waited = wait_any(
            root_dir=runtime_root,
            pool_id=pool_id,
            timeout_seconds=240,
            client=client,
        )
        assert waited["event"] is not None
        assert waited["event"]["kind"] == "candidate_ready"
        pool_closed = close_pool(
            root_dir=runtime_root,
            pool_id=pool_id,
            mode="drain",
            client=client,
        )
        assert pool_closed["state"] == "closed"

        deadline = time.monotonic() + 180
        annotation = None
        while time.monotonic() < deadline:
            annotation = service_thinkthread_annotation(
                root_dir=runtime_root,
                run_id=run_id,
                client=client,
            )
            if annotation is not None and annotation.get("active_task") is not None:
                break
            time.sleep(0.1)
        assert annotation is not None and annotation.get("active_task") is not None
        annotation_child_id = str(annotation["thinkthread_id"])
        claimed = runtime.annotation.task(
            run_id,
            candidate.candidate_id,
            1,
        )
        assert claimed is not None and claimed.attempts == 1
        queue_before = runtime.annotation.queue(run_id)

        selection = runtime.select(run_id)
        selected = True
        assert selection["selected_candidate_id"] == candidate.candidate_id
        queue_after = runtime.annotation.queue(run_id)
        assert queue_after.state == "closed"
        assert queue_after.generation == queue_before.generation + 1
        assert AnnotationHostService(runtime.root_dir).finish_annotation_task(
            claimed,
            result=EvidenceAnnotationResult(
                description="This late annotation must remain unpublished.",
                supplemental_evaluation=None,
                comparison_basis=list(claimed.comparison_basis),
                tool_views=[],
                usage={},
            ),
        ) is False
        cancelled = runtime.annotation.task(
            run_id,
            candidate.candidate_id,
            1,
        )
        assert cancelled is not None
        assert cancelled.state == "cancelled"
        assert cancelled.view is None
        closed_annotation = annotation_snapshot(root_dir=runtime_root, run_id=run_id)
        assert closed_annotation is not None and closed_annotation["status"] == "closed"
        assert client.invoke("thinkthread.list").get("children") == []
    finally:
        if not selected:
            close_pool(
                root_dir=runtime_root,
                pool_id=pool_id,
                mode="interrupt",
                client=client,
            )
            close_thinkthread_annotation(
                root_dir=runtime_root,
                run_id=run_id,
                client=client,
            )

    return {
        "schema_version": 1,
        "scenario": config["scenario"],
        "passed_test_ids": ["TT-LNX-CANCEL-001"],
        "run_id": run_id,
        "candidate_id": candidate.candidate_id,
        "annotation_child_id": annotation_child_id,
        "queue_generation_before": queue_before.generation,
        "queue_generation_after": queue_after.generation,
        "late_result_published": False,
        "cleanup_states": sorted(item["state"] for item in pool_closed["cleanup"]),
    }


class _ApplyDeliveryFault:
    def __init__(
        self,
        delegate: AgentPosixSdkClient,
        *,
        apply_before_loss: bool,
    ) -> None:
        self.delegate = delegate
        self.apply_before_loss = apply_before_loss
        self.hold_status = True
        self.apply_calls = 0
        self.request_id: str | None = None

    def invoke(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        normalized = dict(params or {})
        if operation == "fs.apply":
            self.apply_calls += 1
            self.request_id = str(normalized["requestId"])
            if self.apply_before_loss:
                self.delegate.invoke(operation, normalized, **kwargs)
            raise AgentPosixBridgeError(
                "injected fs.apply response loss",
                error={"delivery": "completion_unknown"},
            )
        if (
            operation == "fs.request.status"
            and self.hold_status
            and normalized.get("requestId") == self.request_id
        ):
            raise AgentPosixBridgeError(
                "injected fs.apply status loss",
                error={"delivery": "completion_unknown"},
            )
        return self.delegate.invoke(operation, normalized, **kwargs)


def _prepare_publication_case(
    *,
    client: AgentPosixSdkClient,
    workspace: Path,
    label: str,
    dependencies: list[dict[str, str]] | None = None,
) -> tuple[FileSearchRuntime, str, Path]:
    case_root = workspace / "publication-cases" / label
    case_root.mkdir(parents=True)
    project = _runtime_project(case_root)
    project.joinpath("notes.txt").write_text("baseline\n", encoding="utf-8")
    runtime_root = workspace / f".gp-tt-publication-{label}"
    payload = _runtime_spec(project).model_dump(mode="json")
    payload["workspace"]["dependencies"] = dependencies or []
    runtime = FileSearchRuntime(runtime_root)
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(payload),
        [project / "evaluator.py"], native_host="pi",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    source_subdir = project.relative_to(workspace).as_posix()
    [candidate] = runtime.start_batch(
        run_id,
        plan.plan_id,
        proposals=[
            CandidateProposal(
                intent=f"Create the isolated {label} publication candidate.",
                hypothesis="The exact private snapshot scores one.",
                instructions=[
                    f"Set {source_subdir}/solution.txt to exactly `1` followed by a newline.",
                    "Call search_run_verifier exactly once, then end the turn.",
                ],
            )
        ],
    )
    opened = open_thinkthread_search_pool(
        root_dir=runtime_root,
        run_id=run_id,
        client=client,
    )
    pool_id = str(opened["pool_id"])
    try:
        waited = wait_any(
            root_dir=runtime_root,
            pool_id=pool_id,
            timeout_seconds=240,
            client=client,
        )
        assert waited["event"] is not None
        assert waited["event"]["kind"] == "candidate_ready"
    finally:
        closed = close_pool(
            root_dir=runtime_root,
            pool_id=pool_id,
            mode="drain",
            client=client,
        )
    assert closed["state"] == "closed"
    selection = runtime.select(run_id)
    assert selection["selected_candidate_id"] == candidate.candidate_id
    manifest = runtime.promote(run_id, candidate.candidate_id)
    assert manifest.is_file()
    promotion = runtime.promotion_record(run_id)
    assert promotion.state == "prepared"
    assert promotion.apply_request_id is not None
    return runtime, run_id, project


def _cleanup_summary(runtime: FileSearchRuntime, run_id: str) -> dict[str, Any]:
    result = runtime.cleanup_thinkthread_snapshots(run_id)
    records = runtime.search_run.state.read_run(run_id).provider_cleanup
    return {
        "state": result["state"],
        "removed": sorted(result["removed"]),
        "retained": sorted(item["snapshot_id"] for item in result["retained"]),
        "record_states": sorted({item.state for item in records}),
        "pins": sorted({pin for item in records for pin in item.pins}),
    }


def run_publication_recovery(config: dict[str, Any]) -> dict[str, Any]:
    client = AgentPosixSdkClient()
    validate_thinkthread_root(client)
    workspace = Path(config["report_path"]).parent

    applied_runtime, applied_run, applied_project = _prepare_publication_case(
        client=client,
        workspace=workspace,
        label="applied",
    )
    applied_project.joinpath("notes.txt").write_text(
        "main unrelated edit\n",
        encoding="utf-8",
    )
    applied = applied_runtime.apply_promotion(applied_run)
    assert applied.state == "applied"
    assert applied_project.joinpath("solution.txt").read_text(encoding="utf-8") == "1\n"
    assert applied_project.joinpath("notes.txt").read_text(encoding="utf-8") == (
        "main unrelated edit\n"
    )
    applied_notifications = applied_runtime.main_agent_notifications(applied_run)
    assert [item.kind for item in applied_notifications] == ["publication_applied"]
    applied_runtime.report(applied_run)
    applied_cleanup = _cleanup_summary(applied_runtime, applied_run)
    assert applied_cleanup["state"] == "complete"
    assert applied_cleanup["record_states"] == ["removed"]

    changed_runtime, changed_run, changed_project = _prepare_publication_case(
        client=client,
        workspace=workspace,
        label="changed-conflict",
    )
    changed_project.joinpath("solution.txt").write_text(
        "root concurrent edit\n",
        encoding="utf-8",
    )
    changed = changed_runtime.apply_promotion(changed_run)
    assert changed.state == "awaiting_main_integration"
    assert changed_project.joinpath("solution.txt").read_text(encoding="utf-8") == (
        "root concurrent edit\n"
    )
    changed_outcome = changed_runtime.search_run.state.read_run(changed_run).publication_outcome
    assert changed_outcome is not None
    assert changed_outcome.apply_outcome == "conflict"
    assert changed_outcome.pending_artifact_path is not None
    assert changed_outcome.pending_artifact_path.is_file()
    changed_notifications = changed_runtime.main_agent_notifications(changed_run)
    assert [item.kind for item in changed_notifications] == [
        "publication_pending_integration"
    ]
    changed_runtime.report(changed_run)
    changed_cleanup = _cleanup_summary(changed_runtime, changed_run)
    assert changed_cleanup["state"] == "retained"
    assert "publication:awaiting_main_integration" in changed_cleanup["pins"]

    dependency_runtime, dependency_run, dependency_project = _prepare_publication_case(
        client=client,
        workspace=workspace,
        label="dependency-conflict",
        dependencies=[{"path": "dependency.txt", "scope": "content"}],
    )
    dependency_project.joinpath("dependency.txt").write_text(
        "root dependency edit\n",
        encoding="utf-8",
    )
    dependency = dependency_runtime.apply_promotion(dependency_run)
    assert dependency.state == "awaiting_main_integration"
    assert dependency_project.joinpath("solution.txt").read_text(encoding="utf-8") == "0\n"
    assert dependency_project.joinpath("dependency.txt").read_text(encoding="utf-8") == (
        "root dependency edit\n"
    )
    dependency_outcome = dependency_runtime.search_run.state.read_run(
        dependency_run
    ).publication_outcome
    assert dependency_outcome is not None
    assert dependency_outcome.apply_outcome == "conflict"
    assert dependency_outcome.pending_artifact_path is not None
    assert dependency_outcome.pending_artifact_path.is_file()

    recovery_runtime, recovery_run, recovery_project = _prepare_publication_case(
        client=client,
        workspace=workspace,
        label="response-loss",
    )
    recovery_fault = _ApplyDeliveryFault(client, apply_before_loss=True)
    recovery_runtime.publication_apply.thinkthread.client = lambda: recovery_fault  # type: ignore[method-assign]
    recovering = recovery_runtime.apply_promotion(recovery_run)
    assert recovering.state == "recovering"
    assert recovery_runtime.main_agent_notifications(recovery_run) == []
    assert not (
        recovery_runtime.search_run.state.run_dir(recovery_run)
        / "artifacts"
        / "pending-integration.json"
    ).exists()
    recovery_request_id = recovering.apply_request_id
    assert recovery_request_id == recovery_fault.request_id
    recovery_fault.hold_status = False
    restarted = FileSearchRuntime(recovery_runtime.root_dir)
    restarted.publication_apply.thinkthread.client = lambda: client  # type: ignore[method-assign]
    recovered = restarted.apply_promotion(recovery_run)
    assert recovered.state == "applied"
    assert recovered.apply_request_id == recovery_request_id
    assert recovery_fault.apply_calls == 1
    assert recovery_project.joinpath("solution.txt").read_text(encoding="utf-8") == "1\n"
    assert [item.kind for item in restarted.main_agent_notifications(recovery_run)] == [
        "publication_applied"
    ]

    fenced_runtime, fenced_run, fenced_project = _prepare_publication_case(
        client=client,
        workspace=workspace,
        label="needs-recovery",
    )
    fenced_fault = _ApplyDeliveryFault(client, apply_before_loss=False)
    fenced_runtime.publication_apply.thinkthread.client = lambda: fenced_fault  # type: ignore[method-assign]
    fenced = fenced_runtime.apply_promotion(fenced_run)
    assert fenced.state == "recovering"
    fenced_restart = FileSearchRuntime(fenced_runtime.root_dir)
    fenced_restart.publication_apply.thinkthread.client = lambda: fenced_fault  # type: ignore[method-assign]
    still_fenced = fenced_restart.apply_promotion(fenced_run)
    assert still_fenced.state == "recovering"
    fenced_outcome = fenced_restart.search_run.state.read_run(fenced_run).publication_outcome
    assert fenced_outcome is not None
    assert fenced_outcome.apply_outcome == "needs_recovery"
    assert fenced_outcome.notification_state == "not_applicable"
    assert fenced_outcome.pending_artifact_path is None
    assert fenced_restart.main_agent_notifications(fenced_run) == []
    assert fenced_fault.apply_calls == 1
    assert fenced_project.joinpath("solution.txt").read_text(encoding="utf-8") == "0\n"

    assert client.invoke("thinkthread.list").get("children") == []
    return {
        "schema_version": 1,
        "scenario": config["scenario"],
        "passed_test_ids": [
            "TT-LNX-APPLY-001",
            "TT-LNX-CONFLICT-001",
            "TT-LNX-CONFLICT-002",
            "TT-LNX-RECOVERY-001",
            "TT-LNX-RECOVERY-002",
            "TT-LNX-CLEANUP-001",
        ],
        "applied": {
            "run_id": applied_run,
            "state": applied.state,
            "request_id": applied.apply_request_id,
            "unrelated_content": applied_project.joinpath("notes.txt").read_text(
                encoding="utf-8"
            ),
            "cleanup": applied_cleanup,
        },
        "changed_conflict": {
            "run_id": changed_run,
            "state": changed.state,
            "request_id": changed.apply_request_id,
            "pending_artifact": str(changed_outcome.pending_artifact_path),
            "cleanup": changed_cleanup,
        },
        "dependency_conflict": {
            "run_id": dependency_run,
            "state": dependency.state,
            "request_id": dependency.apply_request_id,
            "pending_artifact": str(dependency_outcome.pending_artifact_path),
        },
        "response_loss": {
            "run_id": recovery_run,
            "first_state": recovering.state,
            "recovered_state": recovered.state,
            "request_id": recovery_request_id,
            "apply_calls": recovery_fault.apply_calls,
        },
        "needs_recovery": {
            "run_id": fenced_run,
            "state": still_fenced.state,
            "request_id": still_fenced.apply_request_id,
            "apply_calls": fenced_fault.apply_calls,
            "notification_state": fenced_outcome.notification_state,
        },
    }

def run_probe(config: dict[str, Any]) -> dict[str, Any]:
    client = AgentPosixSdkClient()
    meta = client.preflight()
    root = validate_thinkthread_root(client)
    capabilities = sorted(
        str(item["id"])
        for item in root["capabilities"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    children = client.invoke("thinkthread.list").get("children")
    return {
        "schema_version": 1,
        "scenario": config["scenario"],
        "passed_test_ids": ["TT-LNX-BOOT-001", "TT-LNX-REGRESSION-001"],
        "environment": {
            **config["host_evidence"],
            "control_protocol": meta["controlProtocolVersion"],
            "contract_fingerprint": meta["contractFingerprint"],
        },
        "root": {
            "thinkthread_id": root["thinkthreadId"],
            "parent_thinkthread_id": root.get("parentThinkthreadId"),
            "capabilities": capabilities,
            "filesystem": client.invoke("fs.stat"),
            "children_before_search": len(children) if isinstance(children, list) else None,
        },
        "expected_contract": {
            "control_protocol": AGENT_POSIX_CONTROL_PROTOCOL_VERSION,
            "contract_fingerprint": AGENT_POSIX_CONTRACT_FINGERPRINT,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report_path = Path(config["report_path"])
    scenarios = {
        "root_preflight": run_probe,
        "abi_single_child": run_abi_smoke,
        "runtime_k2": run_runtime_k2,
        "runtime_k2_autoresearch": run_runtime_k2,
        "annotation_views": run_annotation_views,
        "annotation_cancel": run_annotation_cancel,
        "publication_recovery": run_publication_recovery,
    }
    if config["scenario"] == "vliw_retained_turns":
        from vliw_scenario import run_vliw_retained_turns

        scenarios["vliw_retained_turns"] = run_vliw_retained_turns
    report = scenarios[config["scenario"]](config)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "report_path": str(report_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
