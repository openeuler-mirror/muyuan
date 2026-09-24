"""Native Codex Main runs the ThinkThread Circle task through goal_plus_host_start."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time

import pytest


pytestmark = [pytest.mark.st, pytest.mark.st_codex, pytest.mark.thinkthread_linux]
ROOT = Path(__file__).resolve().parents[2]
PROFILE = "goal-plus-codex"

RECTANGULAR_START = 2.1666640666666663
STAGGERED_START = 2.4389636673796344


def _output(command: list[str]) -> str:
    return subprocess.run(
        command, cwd=ROOT, check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


def _host_evidence(profile: str) -> dict:
    evidence = {
        "thinkthread_version": _output([os.environ.get("ST_THINKTHREAD_BINARY", "tt"), "--version"]),
        "codex_version": _output([os.environ.get("ST_CODEX_BINARY", "codex"), "--version"]),
        "node_version": _output(["node", "--version"]),
        "checkout_sha": _output(["git", "rev-parse", "HEAD"]),
    }
    shown = subprocess.run(
        [os.environ.get("ST_THINKTHREAD_BINARY", "tt"), "profile", "show", profile],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=30,
    ).stdout
    evidence["profile"] = profile
    evidence["profile_sha256"] = hashlib.sha256(shown.encode()).hexdigest()
    return evidence


def assert_codex_circle_evidence(
    runtime_root: Path,
    run_dir: Path,
    *,
    expected_starts: dict[str, float],
    bounded_iterations: int,
    annotation_required: bool,
) -> dict:
    run = json.loads((run_dir / "run.json").read_text())
    assert run["state"] == "promoted" and run["search_result"] == "passed"
    candidates = {
        path.parent.name: json.loads(path.read_text())
        for path in (run_dir / "candidates").glob("*/candidate.json")
    }
    assert set(candidates) == set(expected_starts)
    sessions = [
        json.loads(path.read_text())
        for path in (run_dir / "agent_sessions").glob("agent_*.json")
    ]
    assert len(sessions) == len(expected_starts)
    assert {item["candidate_id"] for item in sessions} == set(expected_starts)
    assert len({item["session_handle"]["external_id"] for item in sessions}) == len(sessions)
    snapshots = set()
    observations = []
    for session in sessions:
        assert session["agent_harness"] == "codex"
        assert session["runtime_provider"] == "thinkthread"
        assert session["execution_generation"] == 0
        handle = session["session_handle"]
        dispatches = handle["metadata"]["dispatches"]
        candidate = candidates[session["candidate_id"]]
        count = len(candidate["iterations"])
        assert count == bounded_iterations
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
        acknowledged = set(resource["acknowledged_messages"].values())
        assert {item["invocation_id"] for item in dispatches} <= acknowledged
        observations.append(
            {
                "session_id": session["agent_session_id"],
                "child_id": handle["external_id"],
                "invocation_ids": [item["invocation_id"] for item in dispatches],
            }
        )
        assert [item["iteration"] for item in candidate["iterations"]] == list(range(1, count + 1))
        assert candidate["iterations"][0]["score"] == pytest.approx(
            expected_starts[session["candidate_id"]]
        )
        assert candidate["iterations"][0]["process_passed"]
        for iteration in candidate["iterations"]:
            assert iteration["agent_session_id"] == session["agent_session_id"]
            assert iteration["process_passed"]
            artifact = iteration["artifact_ref"]
            assert artifact["provider"] == "thinkthread" and artifact["kind"] == "fs_snapshot"
            assert iteration["metrics"]["_thinkthread_settlement"]["snapshot_id"] == artifact["id"]
            assert iteration["metrics"]["_candidate_attempt"]["artifact_ref"] == artifact
            verifier_metrics = iteration["metrics"].get("circle_packing_score")
            assert verifier_metrics is not None and verifier_metrics["valid"]
            assert verifier_metrics["snapshot_id"] == artifact["id"]
            assert not iteration["changed_outside_allowed"]
            assert not iteration["touched_denied_files"]
            snapshots.add(artifact["id"])
            if not annotation_required:
                continue
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
    assert len(snapshots) == sum(len(candidate["iterations"]) for candidate in candidates.values())
    selected_id = run["selected_candidate_id"]
    selected = candidates[selected_id]["iterations"][run["selected_iteration"] - 1]
    assert selected["process_passed"]
    assert selected["score"] == max(
        item["score"] for candidate in candidates.values() for item in candidate["iterations"]
    )
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
    assert evidence["passed"] and candidates[selected_id]["promotion_report"]["promotion_passed"]
    assert promotion["publication_artifact_ref"] != selected["artifact_ref"]
    assert promotion["apply_result"]["targetSnapshotId"] == evidence["artifact_ref"]["id"]
    assert promotion["apply_result"]["baseSnapshotId"] == run["source_artifact_ref"]["id"]
    assert promotion["changed_files"] == ["initial_program.py"]
    publication = json.loads((run_dir / "promotion" / f"{selected_id}.json").read_text())
    [mutation] = publication["mutations"]
    assert mutation["path"] == "initial_program.py"
    assert (
        hashlib.sha256((Path(run["source_path"]) / mutation["path"]).read_bytes()).hexdigest()
        == mutation["sha256"]
    )
    return {
        "selected_artifact_ref": selected["artifact_ref"],
        "publication_artifact_ref": evidence["artifact_ref"],
        "apply_request_id": promotion["apply_request_id"],
        "sessions": observations,
    }


def _last_codex_assistant_text(state_root: Path) -> str | None:
    """Best-effort native evidence: the Codex rollout of the Root instance."""
    rollouts = sorted(
        state_root.glob("instances/*/thinkthreads/*/session/**/rollout-*.jsonl"),
        key=lambda path: path.stat().st_mtime,
    )
    if not rollouts:
        return None
    last_text = None
    for line in rollouts[-1].read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = entry.get("payload") or {}
        if payload.get("type") != "message" or payload.get("role") != "assistant":
            continue
        text = "".join(
            part.get("text", "")
            for part in payload.get("content", [])
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
        )
        if text.strip():
            last_text = text
    return last_text


def run_codex_circle(
    thinkthread_root,
    prompt: str,
    *,
    expected_starts: dict[str, float],
    bounded_iterations: int,
    annotation_required: bool,
    deadline_seconds: int,
):
    for executable in ("tt", "codex", "node"):
        if not shutil.which(executable):
            pytest.skip(f"missing {executable}")
    base, state_root = thinkthread_root
    workspace = base / "circle"
    example = ROOT / "examples/circle-packing-thinkthread-apply"
    subprocess.run(
        [sys.executable, str(example / "prepare_workspace.py"), "--destination", str(workspace)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    verifier = workspace / ".goal-plus-verifiers/circle_packing_score.py"
    verifier_bytes = verifier.read_bytes()
    profile = os.environ.get("ST_THINKTHREAD_CODEX_PROFILE", PROFILE)
    evidence = _host_evidence(profile)
    command = [
        "tt", "run",
        "--profile", profile,
        "--fs", str(workspace),
        "--state-root", str(state_root),
    ]
    agent_args = []
    if model := os.environ.get("ST_CODEX_MODEL"):
        agent_args.extend(["--model", model])
    if effort := os.environ.get("ST_CODEX_REASONING_EFFORT"):
        agent_args.extend(["-c", f'model_reasoning_effort="{effort}"'])
    if agent_args:
        command.extend(["--", *agent_args])
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))

    def attach_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    log_path = state_root.parent / "native-main.log"
    environment = {**os.environ, "TERM": "xterm-256color"}
    if annotation_required:
        environment["GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED"] = "1"
        environment["GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED"] = "1"
    process = subprocess.Popen(
        command, cwd=ROOT, env=environment,
        stdin=slave, stdout=slave, stderr=slave, preexec_fn=attach_terminal,
    )
    os.close(slave)
    print(f"\nNative Codex TT Circle: {state_root.parent}", flush=True)
    submitted = False
    paste_at = None
    repaste_count = 0
    next_observation = 0.0
    goal_path = None
    runtime_root = None
    root_id = None
    try:
        with log_path.open("wb") as log:
            deadline = time.monotonic() + deadline_seconds
            while time.monotonic() < deadline:
                assert process.poll() is None, f"Native Root exited before completion: {log_path}"
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
                        check=True, capture_output=True, text=True, timeout=30,
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
                    # A failed first turn (e.g. a transient 503 from the model
                    # upstream) leaves the Root idle at an empty input with no
                    # Goal; resubmit the prompt a bounded number of times while
                    # nothing has started yet. goal_plus_host_start is replay
                    # safe, so a duplicate submission cannot fork the Goal.
                    if (
                        submitted
                        and paste_at is not None
                        and now - paste_at > 240
                        and repaste_count < 2
                        and roots
                        and root_state["sessionState"] == "ready"
                    ):
                        os.write(master, b"\x1b[200~" + prompt.encode() + b"\x1b[201~")
                        paste_at = now
                        submitted = False
                        repaste_count += 1
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
                    f"Native Codex Circle did not complete within the external watchdog: {log_path}"
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
        assert {item["candidate_id"] for item in candidates} == set(expected_starts)
        sessions = [
            json.loads(path.read_text())
            for path in (run_dir / "agent_sessions").glob("agent_*.json")
        ]
        assert len(sessions) == len(expected_starts)
        assert all(
            item["agent_harness"] == "codex" and item["runtime_provider"] == "thinkthread"
            for item in sessions
        )
        evidence_result = assert_codex_circle_evidence(
            runtime_root, run_dir,
            expected_starts=expected_starts,
            bounded_iterations=bounded_iterations,
            annotation_required=annotation_required,
        )
        assert (run_dir / "report.md").is_file() and (run_dir / "report.html").is_file()
        assert verifier.read_bytes() == verifier_bytes
        verified = subprocess.run(
            [sys.executable, str(verifier)],
            cwd=workspace, check=True, capture_output=True, text=True, timeout=30,
        )
        score = json.loads(verified.stdout)
        assert score["valid"] and score["sum_radii"] >= 2.2
        final_text = _last_codex_assistant_text(state_root)
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
                    "child_ids": [item["session_handle"]["external_id"] for item in sessions],
                    "evidence": evidence_result,
                    "host_evidence": evidence,
                    "final_assistant_message": final_text,
                    "final_assistant_message_gap": final_text is None,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        if root_id is not None:
            subprocess.run(
                ["tt", "stop", root_id, "--state-root", str(state_root)],
                check=False, capture_output=True, text=True, timeout=60,
            )
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        os.close(master)


def test_native_codex_main_completes_single_candidate_search_thinkthread(thinkthread_root):
    """L3: one bounded Codex candidate, exact Evidence settlement, apply, cleanup."""
    prompt = (
        "mode=probe max_parallel=1 max_duration_seconds=1200 workspace_provider=thinkthread "
        "promotion_mode=apply strategy=agent_guided inner_agent=common "
        "Complete the Circle Packing task in search-spec.json. Freeze the complete SearchSpec "
        "with strategy.inner_agent=\"common\" and budget.max_parallel=1 (this Codex binding "
        "supports only the common inner agent and this is a K=1 run; keep every other field "
        "exactly as in search-spec.json) and freeze "
        ".goal-plus-verifiers/circle_packing_score.py. "
        "Run exactly one K=1 Search with a single candidate: c001 starts by copying "
        "candidate-hints/staggered.py to initial_program.py. The first turn verifies that "
        "implementation with goal_plus_search_run_verifier and returns; no further improvement "
        "turn is required. Then select the best valid exact snapshot, independently verify the "
        "publication and apply it to Root. Re-run the Root verifier and require valid=true and "
        "sum_radii >= 2.2. Finish the Goal audit, mark complete, generate MD/HTML reports and "
        "complete resource cleanup. Keep verifier and other frozen files unchanged."
    )
    run_codex_circle(
        thinkthread_root,
        prompt,
        expected_starts={"c001": STAGGERED_START},
        bounded_iterations=1,
        annotation_required=False,
        deadline_seconds=int(os.environ.get("ST_CODEX_CIRCLE_TIMEOUT", "1500")),
    )


def test_native_codex_main_completes_circle_thinkthread_apply(thinkthread_root):
    """L4: two retained Codex candidates, continuation, annotation, selection, apply."""
    prompt = (
        "mode=probe max_parallel=2 max_duration_seconds=1800 workspace_provider=thinkthread "
        "promotion_mode=apply strategy=agent_guided inner_agent=common "
        "Complete the Circle Packing task in search-spec.json. Freeze the complete SearchSpec "
        "including its Annotation policy, with strategy.inner_agent=\"common\" (this Codex binding "
        "supports only the common inner agent; keep every other field exactly as in "
        "search-spec.json), and freeze .goal-plus-verifiers/circle_packing_score.py. "
        "Run exactly one K=2 Search with two candidates: c001 starts by copying candidate-hints/rectangular.py "
        "to initial_program.py; c002 starts from candidate-hints/staggered.py. Each first turn verifies "
        "that implementation and returns. Reuse each same Child for one further bounded improvement "
        "and verification, then return. Four candidate deliveries are sufficient; no further exploration "
        "is required. Wait for the required Annotation Evidence, select the best valid exact snapshot, "
        "independently verify the publication and apply it to Root. Re-run the Root verifier and require "
        "valid=true and sum_radii >= 2.2. Finish the Goal audit, mark complete, generate MD/HTML reports "
        "and complete resource cleanup. Keep verifier and other frozen files unchanged."
    )
    run_codex_circle(
        thinkthread_root,
        prompt,
        expected_starts={"c001": RECTANGULAR_START, "c002": STAGGERED_START},
        bounded_iterations=2,
        annotation_required=True,
        deadline_seconds=int(os.environ.get("ST_CODEX_CIRCLE_TIMEOUT", "2100")),
    )


def test_native_codex_main_completes_circle_thinkthread_apply_k2_single_turn(thinkthread_root):
    """Reduced L4: two retained Codex candidates, one delivery each, annotation, apply."""
    prompt = (
        "mode=probe max_parallel=2 max_duration_seconds=1200 workspace_provider=thinkthread "
        "promotion_mode=apply strategy=agent_guided inner_agent=common "
        "Complete the Circle Packing task in search-spec.json. Freeze the complete SearchSpec "
        "including its Annotation policy, with strategy.inner_agent=\"common\" (this Codex binding "
        "supports only the common inner agent; keep every other field exactly as in "
        "search-spec.json), and freeze .goal-plus-verifiers/circle_packing_score.py. "
        "Run exactly one K=2 Search with two candidates: c001 starts by copying candidate-hints/rectangular.py "
        "to initial_program.py; c002 starts from candidate-hints/staggered.py. Each candidate verifies "
        "that implementation in its first turn and returns; no further improvement turn is required. "
        "Two candidate deliveries are sufficient. Wait for the required Annotation Evidence, select the "
        "best valid exact snapshot, independently verify the publication and apply it to Root. Re-run the "
        "Root verifier and require valid=true and sum_radii >= 2.2. Finish the Goal audit, mark complete, "
        "generate MD/HTML reports and complete resource cleanup. Keep verifier and other frozen files "
        "unchanged. The promotion call already runs the independent promotion verifier: after "
        "goal_plus_search_apply_promotion returns state=applied, do not call "
        "goal_plus_search_run_verifier again (especially not scope=promotion) and do not repeat "
        "promotion; run only the Root verifier, then audit, close the execution scope, report, and cleanup."
    )
    run_codex_circle(
        thinkthread_root,
        prompt,
        expected_starts={"c001": RECTANGULAR_START, "c002": STAGGERED_START},
        bounded_iterations=1,
        annotation_required=True,
        deadline_seconds=int(os.environ.get("ST_CODEX_CIRCLE_TIMEOUT", "1500")),
    )
