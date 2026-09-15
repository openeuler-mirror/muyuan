"""Native Pi Main runs the complete public ThinkThread Circle task."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time

import pytest


pytestmark = [pytest.mark.st, pytest.mark.st_pi_thinkthread, pytest.mark.thinkthread_linux]
ROOT = Path(__file__).resolve().parents[2]


def assert_acknowledged_invocations(resource: dict, dispatches: list[dict], *, bounded: bool) -> None:
    completed = {item["invocation_id"] for item in dispatches}
    acknowledged = set(resource["acknowledged_messages"].values())
    assert completed <= acknowledged
    unsettled = acknowledged - completed
    if bounded:
        assert not unsettled
    elif unsettled:
        # Main can close an unfinished continuation after another lane meets the goal.
        assert unsettled == {resource["active_message_id"]}
        assert resource["cleanup_state"] == "closed"
        assert resource["status"] == "stopped"
        assert resource.get("invocation_result") is None
        assert resource.get("attempt_request") is None


def assert_circle_evidence(runtime_root: Path, run_dir: Path, *, bounded: bool) -> dict:
    run = json.loads((run_dir / "run.json").read_text())
    assert run["state"] == "promoted" and run["search_result"] == "passed"
    candidates = {
        path.parent.name: json.loads(path.read_text())
        for path in (run_dir / "candidates").glob("*/candidate.json")
    }
    assert set(candidates) == {"c001", "c002"}
    sessions = [
        json.loads(path.read_text())
        for path in (run_dir / "agent_sessions").glob("agent_*.json")
    ]
    assert len(sessions) == 2
    assert {item["candidate_id"] for item in sessions} == set(candidates)
    assert len({item["session_handle"]["external_id"] for item in sessions}) == 2
    observations = []
    annotations = []
    snapshots = set()
    for session in sessions:
        assert session["agent_harness"] == "pi"
        assert session["runtime_provider"] == "thinkthread"
        assert session["execution_generation"] == 0
        handle = session["session_handle"]
        dispatches = handle["metadata"]["dispatches"]
        candidate = candidates[session["candidate_id"]]
        count = len(candidate["iterations"])
        assert (count == 2) if bounded else (count >= 1)
        assert handle["metadata"]["dispatch_count"] == len(dispatches) == count
        assert len({item["invocation_id"] for item in dispatches}) == count
        assert all(
            item["native_session_id"] == handle["external_id"]
            and item["state"] == "completed"
            for item in dispatches
        )
        [resource_path] = (runtime_root / "host-scopes").glob(
            f"*/thinkthread-sessions/{session['agent_session_id']}.json"
        )
        resource = json.loads(resource_path.read_text())
        assert resource["cleanup_state"] == "closed"
        assert resource["thinkthread_id"] == handle["external_id"]
        assert resource["fs_branch_id"] == handle["metadata"]["fs_branch_id"]
        assert_acknowledged_invocations(resource, dispatches, bounded=bounded)
        observations.append(
            {
                "session_id": session["agent_session_id"],
                "child_id": handle["external_id"],
                "invocation_ids": [item["invocation_id"] for item in dispatches],
            }
        )
        assert [item["iteration"] for item in candidate["iterations"]] == list(range(1, count + 1))
        expected_start = 2.1666640666666663 if session["candidate_id"] == "c001" else 2.4389636673796344
        assert candidate["iterations"][0]["score"] == pytest.approx(expected_start)
        assert candidate["iterations"][0]["process_passed"]
        for iteration in candidate["iterations"]:
            assert iteration["agent_session_id"] == session["agent_session_id"]
            artifact = iteration["artifact_ref"]
            assert (
                artifact["provider"] == "thinkthread"
                and artifact["kind"] == "fs_snapshot"
            )
            assert iteration["metrics"]["_thinkthread_settlement"]["snapshot_id"] == artifact["id"]
            assert iteration["metrics"]["_candidate_attempt"]["artifact_ref"] == artifact
            verifier_metrics = iteration["metrics"].get("circle_packing_score")
            if verifier_metrics is not None:
                assert verifier_metrics["snapshot_id"] == artifact["id"]
            if bounded or iteration["process_passed"]:
                assert iteration["process_passed"]
                assert verifier_metrics["valid"]
                assert not iteration["changed_outside_allowed"]
                assert not iteration["touched_denied_files"]
            else:
                assert iteration["score"] == 0.0
                assert iteration["failure_class"]
                assert iteration["disposition"] == "failure"
                restored = iteration["restored_to_iteration"]
                assert 1 <= restored < iteration["iteration"]
                assert candidate["iterations"][restored - 1]["process_passed"]
                assert iteration["restored_to_artifact_ref"] == iteration["workspace_artifact_after_settlement"]
                assert iteration["restored_to_artifact_ref"] != artifact
            snapshots.add(artifact["id"])
            annotation_path = (
                run_dir
                / "candidates"
                / session["candidate_id"]
                / "evidence-annotations"
                / f"iteration-{iteration['iteration']:04d}.json"
            )
            annotation = json.loads(annotation_path.read_text())
            assert (
                annotation["state"] == "completed"
                and annotation["supplemental_evaluation_enabled"]
            )
            assert annotation["settlement_id"] == iteration["settlement_id"]
            assert annotation["attempt_artifact_ref"] == artifact
            assert annotation["view"]["attempt_artifact_ref"] == artifact
            assert annotation["view"]["supplemental_evaluation"]["dimensions"]
            for peer in annotation["comparison_basis"]:
                peer_iteration = candidates[peer["candidate_id"]]["iterations"][
                    peer["iteration"] - 1
                ]
                assert peer["artifact_ref"] == peer_iteration["artifact_ref"]
            annotations.append(annotation["task_id"])
    assert len(snapshots) == sum(len(candidate["iterations"]) for candidate in candidates.values())
    selected_id = run["selected_candidate_id"]
    selected = candidates[selected_id]["iterations"][run["selected_iteration"] - 1]
    assert selected["process_passed"]
    assert selected["score"] == max(item["score"] for candidate in candidates.values()
                                    for item in candidate["iterations"] if item["process_passed"])
    assert run["selected_artifact_ref"] == selected["artifact_ref"]
    assert run["selected_artifact_hash"] == selected["artifact_hash"]
    promotion = run["promotion"]
    evidence = candidates[selected_id]["promotion_evidence"]
    assert promotion["state"] == run["publication_outcome"]["state"] == "applied"
    assert (
        promotion["selected_artifact_ref"]
        == evidence["selected_artifact_ref"]
        == selected["artifact_ref"]
    )
    assert promotion["publication_artifact_ref"] == evidence["artifact_ref"]
    assert (
        evidence["passed"]
        and candidates[selected_id]["promotion_report"]["promotion_passed"]
    )
    assert promotion["publication_artifact_ref"] != selected["artifact_ref"]
    assert (
        promotion["apply_result"]["targetSnapshotId"] == evidence["artifact_ref"]["id"]
    )
    assert (
        promotion["apply_result"]["baseSnapshotId"] == run["source_artifact_ref"]["id"]
    )
    assert promotion["changed_files"] == ["initial_program.py"]
    publication = json.loads((run_dir / "promotion" / f"{selected_id}.json").read_text())
    [mutation] = publication["mutations"]
    assert mutation["path"] == "initial_program.py"
    assert (
        hashlib.sha256(
            (Path(run["source_path"]) / mutation["path"]).read_bytes()
        ).hexdigest()
        == mutation["sha256"]
    )
    native_entries = [
        json.loads(line)
        for line in (runtime_root.parent / "active.jsonl").read_text().splitlines()
    ]
    final = [
        entry
        for entry in native_entries
        if entry["type"] == "message" and entry["message"]["role"] == "assistant"
    ][-1]
    assert final["message"]["stopReason"] == "stop"
    assert any(
        part["type"] == "text" and part["text"].strip()
        for part in final["message"]["content"]
    )
    return {
        "selected_artifact_ref": selected["artifact_ref"],
        "publication_artifact_ref": evidence["artifact_ref"],
        "apply_request_id": promotion["apply_request_id"],
        "annotations": annotations,
        "sessions": observations,
        "final_response_id": final["id"],
    }


@pytest.mark.parametrize("prompt_kind", ["bounded", "example", "three_rounds"])
def test_native_pi_main_completes_circle_thinkthread_apply(thinkthread_root, prompt_kind):
    base, state_root = thinkthread_root
    workspace = base / "circle"
    example = ROOT / "examples/circle-packing-thinkthread-apply"
    subprocess.run(
        [
            sys.executable,
            str(example / "prepare_workspace.py"),
            "--destination",
            str(workspace),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    verifier = workspace / ".goal-plus-verifiers/circle_packing_score.py"
    verifier_bytes = verifier.read_bytes()
    prompt = (
        "/goal-plus mode=probe max_parallel=2 max_duration_seconds=1200 workspace_provider=thinkthread "
        "promotion_mode=apply strategy=agent_guided inner_agent=autoresearch "
        "Complete the Circle Packing task in search-spec.json. Use the complete SearchSpec including "
        "its Annotation policy and frozen .goal-plus-verifiers/circle_packing_score.py. "
        "Run exactly one K=2 Search with two candidates: c001 starts by copying candidate-hints/rectangular.py "
        "to initial_program.py; c002 starts from candidate-hints/staggered.py. Each first turn verifies "
        "that implementation and returns. Reuse each same Child for one further bounded improvement "
        "and verification, then return. Four candidate deliveries are sufficient; no further exploration "
        "is required. Wait for the required Annotation Evidence, select the best valid exact snapshot, "
        "independently verify the publication and apply it to Root. Re-run the Root verifier and require "
        "valid=true and sum_radii >= 2.4. Finish the Goal audit, mark complete, generate MD/HTML reports "
        "and complete resource cleanup. Keep verifier and other frozen files unchanged."
    )
    if prompt_kind == "example":
        prompt = (example / "pi-goal-prompt.md").read_text().strip()
    elif prompt_kind == "three_rounds":
        prompt = prompt.replace("max_duration_seconds=1200", "max_duration_seconds=1800").replace(
            "one further bounded improvement", "two further separately dispatched bounded improvements",
        ).replace("Four candidate deliveries are sufficient", "Six candidate deliveries, three per same retained Child, are required")
    command = [
        "tt",
        "run",
        "--profile",
        os.environ.get("ST_THINKTHREAD_PROFILE", "goal-plus-pi"),
        "--fs",
        str(workspace),
        "--state-root",
        str(state_root),
        "--",
        "--thinking",
        "low",
    ]
    if model := os.environ.get("ST_PI_MODEL"):
        command.extend(["--model", model])
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))

    def attach_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    log_path = state_root.parent / "native-main.log"
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env={
            **{key: value for key, value in os.environ.items()
               if prompt_kind != "three_rounds" or key not in {"GOAL_PLUS_DEBUG", "GOAL_PLUS_SESSION_DIAGNOSTICS"}},
            "TERM": "xterm-256color",
            "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED": "1",
            "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED": "1",
        },
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=attach_terminal,
    )
    os.close(slave)
    print(f"\nNative Pi TT Circle: {state_root.parent}", flush=True)
    submitted = False
    paste_at = None
    next_observation = 0.0
    goal_path = None
    runtime_root = None
    root_id = None
    try:
        with log_path.open("wb") as log:
            deadline = time.monotonic() + (2100 if prompt_kind == "three_rounds" else 1500)
            while time.monotonic() < deadline:
                assert process.poll() is None, (
                    f"Native Root exited before completion: {log_path}"
                )
                if select.select([master], [], [], 0.2)[0]:
                    chunk = os.read(master, 65536)
                    log.write(chunk)
                    log.flush()
                now = time.monotonic()
                if paste_at is not None and not submitted and now - paste_at >= 0.5:
                    os.write(master, b"\r")
                    submitted = True
                if now < next_observation:
                    continue
                next_observation = now + 2
                native = json.loads(
                    subprocess.run(
                        ["tt", "ps", "--json", "--state-root", str(state_root)],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    ).stdout
                )["thinkthreads"]
                roots = [item for item in native if item["parentId"] is None]
                if roots:
                    [root_state] = roots
                    root_id = root_state["id"]
                    assert root_state["state"] != "stopped", root_state
                    if paste_at is None and root_state["sessionState"] == "ready":
                        os.write(master, b"\x1b[200~" + prompt.encode() + b"\x1b[201~")
                        paste_at = now
                goals = list(
                    state_root.glob(
                        "instances/*/thinkthreads/*/session/**/goal-plus/gp_*/goal.json"
                    )
                )
                if not goals:
                    continue
                [goal_path] = goals
                runtime_root = goal_path.parents[2]
                goal = json.loads(goal_path.read_text())
                scopes = [
                    json.loads(path.read_text())
                    for path in (runtime_root / "host-scopes").glob("*/scope.json")
                ]
                reports = list((runtime_root / "runs").glob("*/report.html"))
                if (
                    goal["status"] == "complete"
                    and reports
                    and scopes
                    and all(item["state"] == "closed" for item in scopes)
                    and roots
                    and root_state["sessionState"] == "ready"
                ):
                    break
                assert goal["status"] not in {"failed", "abandoned", "needs_user"}, goal
            else:
                pytest.fail(
                    f"Native Circle did not complete within the external watchdog: {log_path}"
                )
        assert submitted and goal_path and runtime_root
        goal = json.loads(goal_path.read_text())
        assert goal["entrypoint_status"] == "host_verified"
        [run_path] = (runtime_root / "runs").glob("*/run.json")
        run_dir = run_path.parent
        run = json.loads(run_path.read_text())
        assert len(list((run_dir / "plans").glob("plan_*.json"))) == 1
        candidates = [
            json.loads(path.read_text())
            for path in (run_dir / "candidates").glob("*/candidate.json")
        ]
        assert {item["candidate_id"] for item in candidates} == {"c001", "c002"}
        minimum = {"bounded": 2, "example": 1, "three_rounds": 3}[prompt_kind]
        assert all(len(item["iterations"]) >= minimum for item in candidates)
        sessions = [
            json.loads(path.read_text())
            for path in (run_dir / "agent_sessions").glob("agent_*.json")
        ]
        assert len(sessions) == 2
        assert len({item["session_handle"]["external_id"] for item in sessions}) == 2
        assert all(item["agent_harness"] == "pi" and item["runtime_provider"] == "thinkthread"
                   for item in sessions)
        evidence = assert_circle_evidence(runtime_root, run_dir, bounded=prompt_kind == "bounded")
        if prompt_kind == "three_rounds":
            assert_circle_diagnostics(runtime_root, sessions, state_root.parent)
        assert (run_dir / "report.md").is_file() and (run_dir / "report.html").is_file()
        assert verifier.read_bytes() == verifier_bytes
        verified = subprocess.run(
            [sys.executable, str(verifier)],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        score = json.loads(verified.stdout)
        assert score["valid"] and score["sum_radii"] >= 2.4
        (state_root.parent / "report.json").write_text(
            json.dumps(
                {
                    "goal_id": goal["goal_plus_id"],
                    "run_id": run["run_id"],
                    "score": score,
                    "runtime_root": str(runtime_root),
                    "root_id": root_id,
                    "cleanup_state": "closed",
                    "session_ids": [item["agent_session_id"] for item in sessions],
                    "child_ids": [
                        item["session_handle"]["external_id"] for item in sessions
                    ],
                    "evidence": evidence,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        if root_id is not None:
            subprocess.run(
                ["tt", "stop", root_id, "--state-root", str(state_root)],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        os.close(master)


def assert_circle_diagnostics(runtime_root, sessions, output):
    import importlib.util

    spec = importlib.util.spec_from_file_location("circle_session_audit", ROOT / "scripts/check_session_instructions.py")
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    overview = (ROOT / "assets/goal-plus-instructions.txt").read_text()
    main = audit.audit_session(runtime_root.parent / "active.jsonl", agent_harness="pi", overview=overview, tools=True)
    assert main["status"] == "present", main["errors"]
    assert main["tool_analysis"]["status"] == "observed"
    assert all(request["request_bytes"] is None for request in main["tool_analysis"]["requests"])
    results = {"main": main}
    for session in sessions:
        child = session["session_handle"]["external_id"]
        directory = runtime_root / "host-logs/session-diagnostics" / child
        index = json.loads((directory / "index.json").read_text())
        assert index["identity"]["agent_session_id"] == session["agent_session_id"]
        assert index["collection_complete"]
        assert len(index["batches"]) >= 3
        assert len({batch["stream_id"] for batch in index["batches"].values()}) == 1
        archived_ids = []
        for digest in index["batches"]:
            batch = json.loads((directory / f"{digest}.json").read_text())
            archived_ids.extend(entry["id"] for entry in batch["entries"])
        assert len(archived_ids) == len(set(archived_ids)), "Continuation must export only new entries"
        analysis = audit.audit_session(directory, agent_harness="pi", overview=overview, tools=True)
        tools = analysis["tool_analysis"]
        assert tools["status"] == "observed", analysis["errors"]
        assert len(tools["requests"]) >= 3
        assert len({request["request_id"] for request in tools["requests"]}) == len(tools["requests"])
        assert all(request["request_bytes"] is None for request in tools["requests"])
        assert tools["summary"]["schema_invalid_calls"] == 0
        results[child] = analysis
    (output / "session-diagnostics.json").write_text(json.dumps(results, indent=2))
