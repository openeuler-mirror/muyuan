from __future__ import annotations


import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from goal_plus.ports.annotation import EvidenceAnnotationResult
from goal_plus.annotation_host import AnnotationHostService
from goal_plus.internal_agents import kick_pi_internal_agents
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import EvidenceViewRecord, SearchSpec, SupplementalEvaluation
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.pi_tool import call_pi_tool
from goal_plus.runtime import FileSearchRuntime
from goal_plus.thinkthread_artifacts import ThinkThreadSnapshotReader
from goal_plus.thinkthread_annotation import (
    ANNOTATION_TOOLS,
    _process_message,
    annotation_snapshot,
    close_thinkthread_annotation,
    kick_thinkthread_annotation_service,
    kick_thinkthread_annotation,
    serve_thinkthread_annotation,
    service_thinkthread_annotation,
)
from tests._runtime_helpers import make_project, start_host_verified_goal
from tests._thinkthread_sdk import IntegratedAgentPosix, thinkthread_spec
from tests._thinkthread_candidates import bind_candidate, verify_candidate, close_candidate




def test_thinkthread_annotation_rpc_allowlist_is_read_only() -> None:
    assert ANNOTATION_TOOLS == {
        "goal_plus_thinkthread_annotation_next",
        "goal_plus_thinkthread_artifact_view_list",
        "goal_plus_thinkthread_artifact_view_stat",
        "goal_plus_thinkthread_artifact_view_pread",
        "goal_plus_thinkthread_artifact_view_diff",
        "goal_plus_thinkthread_annotation_submit",
    }






@pytest.mark.parametrize("executable", ["python", "python3"])
def test_thinkthread_verifier_uses_runtime_python_before_host_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executable: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    evaluator = project / "evaluator.py"
    evaluator.write_text(
        "import json, sys\nprint(json.dumps({'combined_score': 42, 'python': sys.executable, 'args': sys.argv[1:]}))\n"
    )
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    command = foreign / executable
    command.write_text("#!/bin/sh\nexit 86\n")
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(foreign) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GOAL_PLUS_PYTHON", str(command))
    client = IntegratedAgentPosix(project)
    original_invoke = client.invoke
    observed = []

    def invoke(operation, params=None, **kwargs):
        result = original_invoke(operation, params, **kwargs)
        if operation == "fs.run":
            invocation = params["invocation"]
            argv = invocation["argv"]
            # Emulate TT resolving argv[0] on the host before applying guest env.
            completed = subprocess.run(
                [shutil.which(argv[0]), *argv[1:]], cwd=project,
                env=invocation["environment"], capture_output=True, timeout=10,
            )
            result["exit"] = {"kind": "code", "code": completed.returncode}
            result["outputChunks"] = [{"sequence": 0, "stream": "stdout",
                "dataBase64": base64.b64encode(completed.stdout).decode("ascii")}]
            observed.append(completed)
        return result

    monkeypatch.setattr(client, "invoke", invoke)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    spec = thinkthread_spec(project)
    arguments = ["a b", "$(touch unexpected)", "X=value"]
    spec.process_verifiers[0].command = [executable, "evaluator.py", *arguments]
    runtime = FileSearchRuntime(tmp_path.parent / (tmp_path.name + "-session-runtime"))
    frozen = runtime.freeze_spec(spec, [evaluator], agent_harness="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    run = runtime.search_run.state.read_run(run_id)
    assert run.workspace_bootstrap_verification.aggregate_score == 42
    assert json.loads(observed[0].stdout) == {"combined_score": 42, "python": sys.executable, "args": arguments}
    assert not project.joinpath("unexpected").exists()
    assert frozen.spec.process_verifiers[0].command == spec.process_verifiers[0].command


def test_thinkthread_autoresearch_launch_uses_pi_prompt_and_private_git_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path.parent / (tmp_path.name + "-session-runtime"))
    payload = thinkthread_spec(project).model_dump(mode="json")
    payload["budget"]["max_parallel"] = 1
    payload["strategy"]["inner_agent"] = "autoresearch"

    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(payload),
        [project / "evaluator.py"], agent_harness="pi",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)

    binding = plan.worker_policy["inner_agent_binding"]
    assert frozen.spec.strategy.inner_agent == "autoresearch"
    assert binding["inner_agent_id"] == "autoresearch"
    assert binding["adapter_version"] == "builtin-autoresearch-git-v1"
    assert binding["runtime_provider"] == "thinkthread"
    assert binding["prompt_asset"] == "assets/pi/prompts/search-candidate-worker.md"
    assert binding["workspace_ledger_projection"] == "results_tsv"
    assert "# Search Candidate Worker" in session.launch["message"]
    assert "ThinkThread Child" in session.launch["message"]
    assert "继续当前自主循环" in session.launch["message"]
    assert "不要只读取 context/Evidence 就结束" in session.launch["message"]
    instructions = "\n".join(task.instructions)
    assert "继续同一条自主搜索循环" in instructions
    assert "不要仅因一次失败或同分而停止" in instructions
    assert "当前内层实现是 AutoResearch-Git" in instructions
    assert "本次 host dispatch 只完成一个有界 attempt" not in instructions

    context = runtime.get_agent_context(session.agent_session_id)
    assert context["inner_agent"] == "autoresearch"
    assert context["result_ledger_source"] == "gp"
    assert context["workspace_ledger_projection"] == "results_tsv"
    assert context["results_tsv"] == "results.tsv"
    assert not task.workspace.exists()








def annotation_request(
    state: dict[str, Any],
    request_id: str,
    tool: str,
    params: dict[str, Any],
    *,
    deadline_unix_ms: int | None = None,
) -> dict[str, Any]:
    content = {
        "agent_session_id": state["agent_session_id"],
        "params": params,
        "tool": tool,
    }
    if deadline_unix_ms is not None:
        content["deadline_unix_ms"] = deadline_unix_ms
    content_json = json.dumps(content, separators=(",", ":"))
    request = {
        "protocol": "goal-plus.thinkthread.v3",
        "type": "request",
        "request_id": request_id,
        "agent_session_id": state["agent_session_id"],
        "tool": tool,
        "params": params,
        "content_json": content_json,
        "content_sha256": hashlib.sha256(content_json.encode()).hexdigest(),
    }
    if deadline_unix_ms is not None:
        request["deadline_unix_ms"] = deadline_unix_ms
    return request


def response_for(client: IntegratedAgentPosix, request_id: str) -> dict[str, Any]:
    chunks: list[dict[str, Any]] = []
    for sent in client.sent:
        text = sent.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "response_chunk" and payload.get("request_id") == request_id:
            chunks.append(payload)
    assert chunks
    unique = {int(item["chunk_index"]): item for item in chunks}
    data = b"".join(
        base64.b64decode(unique[index]["data_base64"])
        for index in range(int(chunks[0]["chunk_count"]))
    )
    assert hashlib.sha256(data).hexdigest() == chunks[0]["response_sha256"]
    return json.loads(data)


def test_thinkthread_annotation_rpc_rejects_execution_before_provider_call(
    tmp_path: Path,
) -> None:
    client = IntegratedAgentPosix(tmp_path)
    state = {
        "agent_session_id": "as-annotation",
        "registered_at": "2026-09-05T00:00:00Z",
    }
    request = annotation_request(
        state,
        "req-forbidden-run",
        "thinkthread_artifact_view_run",
        {"view_id": "view-target", "argv": ["python", "evaluator.py"]},
    )

    with pytest.raises(PermissionError, match="RPC tool is not allowed"):
        _process_message(
            client,  # type: ignore[arg-type]
            tmp_path,
            state,
            {"text": json.dumps(request)},
        )

    assert client.operations == []






def test_pi_facade_and_worker_message_proxy_preserve_search_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    from goal_plus.host_scope import ensure_execution_scope, close_execution_scope
    from goal_plus.session_host import NativeCandidateHost

    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    monkeypatch.setattr(
        "goal_plus.infrastructure.thinkthread_sessions.AgentPosixSdkClient",
        lambda: client,
    )
    monkeypatch.setattr("goal_plus.thinkthread_annotation.AgentPosixSdkClient", lambda: client)
    monkeypatch.setenv("GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED", "1")
    root_dir = tmp_path.parent / (tmp_path.name + "-session-runtime")
    spec_payload = thinkthread_spec(project).model_dump(mode="json")
    spec_payload["budget"]["max_parallel"] = 1
    spec_payload["strategy"]["config"]["global_evidence_mode"] = "auto"
    start_host_verified_goal(root_dir, project, agent_harness="pi")
    goal = FileGoalPlusRuntime(root_dir).require_host_verified_active_goal()

    frozen = call_pi_tool(
        root_dir,
        "goal_plus_search_freeze_spec",
        {
            "goal_plus_id": goal.goal_plus_id,
            "spec": spec_payload,
            "verifier_artifact_paths": ["evaluator.py"],
        },
    )
    assert FileSearchRuntime(root_dir).search_run.state.read_frozen_spec(
        frozen["frozen_spec_id"]
    ).spec.strategy.config["global_evidence_mode"] == "auto"
    run_id = call_pi_tool(
        root_dir,
        "goal_plus_search_create",
        {
            "request_id": "initial",
            "goal_plus_id": goal.goal_plus_id,
            "frozen_spec_id": frozen["frozen_spec_id"],
        },
    )["run_id"]
    plan = call_pi_tool(
        root_dir,
        "goal_plus_search_plan_next",
        {"run_id": run_id, "requested_k": 1},
    )
    tasks = call_pi_tool(
        root_dir,
        "goal_plus_search_start_batch",
        {"run_id": run_id, "plan_id": plan["plan_id"]},
    )
    candidate_id = tasks[0]["candidate_id"]
    agent_session_id = FileSearchRuntime(root_dir).start_agent_session(run_id, candidate_id).agent_session_id
    scope = ensure_execution_scope(
        root_dir=root_dir, agent_harness="pi", owner_session_id=goal.active_session.session_id,
         run_id=run_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
    )
    agent_harness = NativeCandidateHost(root_dir, agent_harness="pi", caller=lambda: goal.active_session.session_id)

    def cleanup():
        agent_harness.shutdown()
        closed = close_execution_scope(root_dir=root_dir, scope_id=scope["scope_id"], reason="test finished")
        assert closed["state"] == "closed", closed

    request.addfinalizer(cleanup)
    agent_harness.open(agent_session_id)
    control = agent_harness(agent_session_id, "wake")
    control.wake("one attempt", call_id="first", timeout=30)
    deadline = time.monotonic() + 5
    while control.wait("first", timeout=0).native_session_id is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    child_id = control.session.native_session_id
    job = control.session.record
    client.enqueue(
        child_id,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "registration",
            "registration_nonce": job["registration_nonce"],
        },
    )

    initial_requests = {
        "req-context-initial": (
            "goal_plus_search_get_agent_context",
            {"agent_session_id": agent_session_id},
        ),
        "req-evidence-initial": (
            "goal_plus_search_get_global_evidence",
            {"agent_session_id": agent_session_id},
        ),
        "req-iterations-initial": (
            "goal_plus_search_list_iterations",
            {"agent_session_id": agent_session_id},
        ),
        "req-verifier": (
            "goal_plus_search_run_verifier",
            {
                "run_id": run_id,
                "candidate_id": candidate_id,
                "scope": "process",
                "agent_session_id": agent_session_id,
                "hypothesis": "Exercise the deferred verifier contract",
                "toolization_decision": {
                    "outcome": "not_applicable",
                    "signals": [],
                    "exclusion": "no_toolizable_material",
                    "rationale": "No reusable material was created.",
                    "tool_names": [],
                },
            },
        ),
    }
    for request_id, (tool, params) in initial_requests.items():
        client.enqueue(
            child_id,
            annotation_request(job, request_id, tool, params),
        )
    wait_for_response(client, "req-verifier", timeout_seconds=5)
    assert control.wait("first", timeout=0).state == "running"
    monkeypatch.setenv("GOAL_PLUS_PI_ROLE", "worker")
    monkeypatch.setenv("GOAL_PLUS_AGENT_SESSION_ID", agent_session_id)
    assert response_for(client, "req-context-initial")["result"] == call_pi_tool(
        root_dir,
        "goal_plus_search_get_agent_context",
        {"agent_session_id": agent_session_id},
    )
    assert response_for(client, "req-evidence-initial")["result"] == []
    assert response_for(client, "req-iterations-initial")["result"] == {"items": [], "total": 0, "next_offset": None}
    verifier_receipt = response_for(client, "req-verifier")["result"]
    assert verifier_receipt["state"] == "accepted"
    assert verifier_receipt["request_id"] == "req-verifier"
    assert verifier_receipt["attempt_id"].startswith("attempt_")
    assert verifier_receipt["run_id"] == run_id
    assert verifier_receipt["candidate_id"] == candidate_id
    assert verifier_receipt["agent_session_id"] == agent_session_id
    assert verifier_receipt["toolization_decision"] is None
    assert "next wake" in verifier_receipt["message"]
    assert "global_evidence_snapshot" not in verifier_receipt

    client.finish(child_id)
    settled = control.wait("first", timeout=10)
    assert settled.state == "completed", settled
    attempt_result = json.loads(settled.result.text)
    assert attempt_result == control.session.record["attempt_result"]
    assert attempt_result["attempt_id"] == verifier_receipt["attempt_id"]
    assert attempt_result["attempt_iteration"] == 1
    assert attempt_result["score_report"]["attempt_iteration"] == 1
    assert (
        attempt_result["attempt_artifact_ref"]
        == attempt_result["score_report"]["attempt_artifact_ref"]
    )
    runtime = FileSearchRuntime(root_dir)
    annotation_task = runtime.annotation.task(run_id, candidate_id, 1)
    assert annotation_task is not None
    runtime.annotation.tasks.write_task(
        annotation_task.model_copy(
            update={
                "state": "completed",
                "view": EvidenceViewRecord(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    iteration=1,
                    attempt_commit=annotation_task.attempt_commit,
                    attempt_artifact_ref=annotation_task.attempt_artifact_ref,
                    description="The snapshot improves the measured value.",
                    supplemental_evaluation=SupplementalEvaluation.model_validate(
                        {
                            "summary": "The exact snapshot improves the metric.",
                            "dimensions": [
                                {
                                    "name": "Snapshot behavior",
                                    "finding": "The verifier reads the changed value.",
                                    "confidence": "high",
                                    "evidence": ["exact snapshot verifier"],
                                }
                            ],
                        }
                    ),
                    comparison_basis=list(annotation_task.comparison_basis),
                    created_at="2026-09-03T00:00:00Z",
                ),
            }
        )
    )
    control.wake("continue", call_id="second", timeout=30)
    deadline = time.monotonic() + 5
    while not any(item.get("wake") is True and item.get("recipientThinkthreadId") == child_id for item in client.sent):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert control.session.native_session_id == child_id
    assert job["dispatch_index"] == 2

    continued_requests = {
        "req-context-continued": (
            "goal_plus_search_get_agent_context",
            {"agent_session_id": agent_session_id},
        ),
        "req-evidence-continued": (
            "goal_plus_search_get_global_evidence",
            {"agent_session_id": agent_session_id},
        ),
        "req-detail-continued": (
            "goal_plus_search_get_evidence_detail",
            {
                "agent_session_id": agent_session_id,
                "candidate_id": candidate_id,
                "iteration": 1,
            },
        ),
        "req-iterations-continued": (
            "goal_plus_search_list_iterations",
            {"agent_session_id": agent_session_id},
        ),
    }
    for request_id, (tool, params) in continued_requests.items():
        client.enqueue(
            child_id,
            annotation_request(job, request_id, tool, params),
        )
    wait_for_response(client, "req-iterations-continued", timeout_seconds=5)

    continued_context = response_for(client, "req-context-continued")["result"]
    assert continued_context == call_pi_tool(
        root_dir,
        "goal_plus_search_get_agent_context",
        {"agent_session_id": agent_session_id},
    )
    assert continued_context["recent_iterations"][-1]["iteration"] == 1
    assert (
        continued_context["recent_iterations"][-1]["attempt_id"]
        == attempt_result["attempt_id"]
    )
    assert (
        continued_context["recent_iterations"][-1]["score"]
        == attempt_result["score_report"]["aggregate_score"]
    )
    assert (
        continued_context["recent_iterations"][-1]["disposition"]
        == attempt_result["score_report"]["disposition"]
    )
    assert continued_context["global_evidence_injected"] is True
    assert continued_context["global_evidence_entry_count"] == 1
    proxy_evidence = response_for(client, "req-evidence-continued")["result"]
    assert proxy_evidence == call_pi_tool(
        root_dir,
        "goal_plus_search_get_global_evidence",
        {"agent_session_id": agent_session_id},
    )
    assert [(item["candidate_id"], item["iteration"]) for item in proxy_evidence] == [
        (candidate_id, 1)
    ]
    assert continued_context["global_evidence_snapshot"] == proxy_evidence
    detail_response = response_for(client, "req-detail-continued")
    assert detail_response["ok"] is True, detail_response
    assert detail_response["result"] == call_pi_tool(
        root_dir,
        "goal_plus_search_get_evidence_detail",
        {
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "iteration": 1,
        },
    )
    assert response_for(client, "req-iterations-continued")["result"] == call_pi_tool(
        root_dir,
        "goal_plus_search_list_iterations",
        {"agent_session_id": agent_session_id},
    )


def wait_for_response(
    client: IntegratedAgentPosix,
    request_id: str,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            return response_for(client, request_id)
        except AssertionError:
            pass
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for annotation response {request_id}")








def test_annotation_service_leaves_process_groups_to_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_dir = tmp_path.parent / (tmp_path.name + "-session-runtime")
    run_id = "run_sandboxed"
    service_root = (
        root_dir / "host-pools" / "thinkthread-annotations" / run_id
    )
    service_root.mkdir(parents=True)
    (service_root / "state.json").write_text(
        json.dumps({"run_id": run_id, "status": "running"}),
        encoding="utf-8",
    )
    launches: list[dict[str, Any]] = []

    class Process:
        pid = 4321

    def launch(_command: list[str], **options: Any) -> Process:
        launches.append(options)
        return Process()

    monkeypatch.setattr(
        "goal_plus.thinkthread_annotation.subprocess.Popen",
        launch,
    )

    assert kick_thinkthread_annotation_service(
        root_dir=root_dir,
        run_id=run_id,
    )
    assert len(launches) == 1
    assert "start_new_session" not in launches[0]
    service = json.loads((service_root / "service.json").read_text(encoding="utf-8"))
    assert service["pid"] == 4321
    assert "start_new_session" not in service


@pytest.mark.parametrize("previous_status", ["exited", "released", "running", None])
def test_annotation_service_restarts_from_terminal_record_in_protected_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_status: str | None,
) -> None:
    root_dir = tmp_path.parent / (tmp_path.name + "-session-runtime")
    run_id = "run_retained_annotation"
    service_root = root_dir / "host-pools" / "thinkthread-annotations" / run_id
    service_root.mkdir(parents=True)
    child_state = {"run_id": run_id, "status": "running", "active_task": {"attempts": 1}}
    (service_root / "state.json").write_text(json.dumps(child_state), encoding="utf-8")
    previous = {"run_id": run_id, "generation": "old-generation", "pid": 4321}
    if previous_status is not None:
        previous["status"] = previous_status
    (service_root / "service.json").write_text(json.dumps(previous), encoding="utf-8")
    launches = []

    class Process:
        pid = 5432

    def launch(command: list[str], **_options: Any) -> Process:
        launches.append(command)
        return Process()

    def denied_signal(*_args: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    # A protected Root may refuse signal probes even after the owned loop exits.
    monkeypatch.setattr("goal_plus.thinkthread_annotation.os.kill", denied_signal)
    monkeypatch.setattr("goal_plus.thinkthread_annotation.subprocess.Popen", launch)

    restarted = kick_thinkthread_annotation_service(root_dir=root_dir, run_id=run_id)
    terminal = previous_status in {"exited", "released"}
    assert restarted is terminal
    current = json.loads((service_root / "service.json").read_text(encoding="utf-8"))
    if terminal:
        assert len(launches) == 1
        assert current["generation"] != previous["generation"]
        assert current["pid"] == 5432
        assert current["status"] == "running"
        assert not kick_thinkthread_annotation_service(root_dir=root_dir, run_id=run_id)
        assert len(launches) == 1
    else:
        assert not launches
        assert current == previous
    assert json.loads((service_root / "state.json").read_text()) == child_state


def test_closed_annotation_service_does_not_recheck_terminal_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_dir = tmp_path.parent / (tmp_path.name + "-session-runtime")
    run_id = "run_already_stopped"
    service_root = (
        root_dir / "host-pools" / "thinkthread-annotations" / run_id
    )
    service_root.mkdir(parents=True)
    (service_root / "state.json").write_text(
        json.dumps({"run_id": run_id, "status": "closed"}),
        encoding="utf-8",
    )
    (service_root / "service.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "generation": "generation-1",
                "pid": 4321,
                "status": "released",
                "finished_at": "2026-09-04T12:48:23Z",
            }
        ),
        encoding="utf-8",
    )

    def unexpected_process_check(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("terminal annotation service PID was rechecked")

    monkeypatch.setattr(
        "goal_plus.thinkthread_annotation.os.kill",
        unexpected_process_check,
    )

    closed = close_thinkthread_annotation(
        root_dir=root_dir,
        run_id=run_id,
        timeout_seconds=0,
    )

    assert closed == {"run_id": run_id, "status": "closed"}
    service = json.loads((service_root / "service.json").read_text(encoding="utf-8"))
    assert service["status"] == "released"
    assert service["finished_at"] == "2026-09-04T12:48:23Z"


def test_annotation_service_reclaims_expired_turn_and_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path.parent / (tmp_path.name + "-session-runtime"))
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], agent_harness="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    sessions = [bind_candidate(runtime, client, run_id, candidate_id)
                for candidate_id in ["c001"]]
    for session in sessions:
        verify_candidate(runtime, client, session)
    kick_thinkthread_annotation(root_dir=runtime.root_dir, run_id=run_id, client=client)

    state = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert state is not None
    annotation_child = str(state["thinkthread_id"])
    client.enqueue(
        annotation_child,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "registration",
            "registration_nonce": state["registration_nonce"],
        },
    )
    expiring_request = annotation_request(
        state,
        "req-expiring-next",
        "goal_plus_thinkthread_annotation_next",
        {"run_id": run_id},
        deadline_unix_ms=int(time.time() * 1000) + 60_000,
    )
    client.enqueue(annotation_child, expiring_request)
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    response = response_for(client, "req-expiring-next")
    assert response["result"]["state"] == "claimed"

    state_path = (
        runtime.root_dir
        / "host-pools"
        / "thinkthread-annotations"
        / run_id
        / "state.json"
    )
    before_replay = json.loads(state_path.read_text(encoding="utf-8"))[
        "pending_responses"
    ]["req-expiring-next"]
    client.enqueue(annotation_child, expiring_request)
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    after_replay = json.loads(state_path.read_text(encoding="utf-8"))[
        "pending_responses"
    ]["req-expiring-next"]
    assert after_replay["queued_at_unix"] == before_replay["queued_at_unix"]
    assert after_replay["expires_at_unix"] == before_replay["expires_at_unix"]

    task = runtime.annotation.task(run_id, "c001", 1)
    assert task is not None
    runtime.annotation.tasks.write_task(
        task.model_copy(update={"outer_deadline_at": "1970-01-01T00:00:00Z"})
    )
    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_state["pending_responses"]["req-expiring-next"]["expires_at_unix"] = 0
    state_path.write_text(json.dumps(raw_state), encoding="utf-8")

    recovered = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert recovered is not None
    assert recovered["status"] == "ready"
    assert recovered["active_task"] is None
    assert recovered["last_turn_recovery"]["deadline_expired"] is True
    assert "req-expiring-next" not in recovered["pending_responses"]
    expired = recovered["expired_responses"]["req-expiring-next"]
    assert expired["reason"] == "child_request_deadline_elapsed"
    assert runtime.annotation.task(run_id, "c001", 1).state == (
        "terminal_error"
    )
    assert any(operation == "thinkthread.signal" for operation, _ in client.operations)

    client.enqueue(
        annotation_child,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "response_ack",
            "request_id": "req-expiring-next",
            "response_sha256": expired["response_sha256"],
        },
    )
    acknowledged = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert acknowledged is not None
    assert acknowledged["expired_responses"]["req-expiring-next"].get(
        "acknowledged_after_expiry_at"
    )

    for session in sessions:
        close_candidate(client, session)
    closed = close_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert closed is not None and closed["status"] == "closed"


def test_annotation_service_handles_rpc_after_model_pause_without_main_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path.parent / (tmp_path.name + "-session-runtime"))
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], agent_harness="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    sessions = [bind_candidate(runtime, client, run_id, candidate_id)
                for candidate_id in ["c001"]]
    for session in sessions:
        verify_candidate(runtime, client, session)
    kick_thinkthread_annotation(root_dir=runtime.root_dir, run_id=run_id, client=client)

    state = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert state is not None
    annotation_child = str(state["thinkthread_id"])
    client.enqueue(
        annotation_child,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "registration",
            "registration_nonce": state["registration_nonce"],
        },
    )
    client.enqueue(
        annotation_child,
        annotation_request(
            state,
            "req-delayed-next",
            "goal_plus_thinkthread_annotation_next",
            {"run_id": run_id},
        ),
    )
    result: list[int] = []
    service = threading.Thread(
        target=lambda: result.append(
            serve_thinkthread_annotation(
                root_dir=runtime.root_dir,
                run_id=run_id,
                client=client,  # type: ignore[arg-type]
                poll_seconds=0.01,
            )
        )
    )
    service.start()
    context = wait_for_response(client, "req-delayed-next")["result"]["task"]
    target_view = context["artifact_views"][0]["view_id"]

    # The real annotator may think for seconds before its next tool call. The
    # host-side service must remain available without another Main tool call.
    time.sleep(0.1)
    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert current is not None
    client.enqueue(
        annotation_child,
        annotation_request(
            current,
            "req-delayed-read",
            "goal_plus_thinkthread_artifact_view_pread",
            {
                "view_id": target_view,
                "path": "project/initial_program.py",
                "offset": 0,
                "length": 64 * 1024,
            },
        ),
    )
    read = wait_for_response(client, "req-delayed-read")
    assert base64.b64decode(read["result"]["dataBase64"]) == b"VALUE = 2\n"

    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert current is not None
    client.enqueue(
        annotation_child,
        annotation_request(
            current,
            "req-delayed-submit",
            "goal_plus_thinkthread_annotation_submit",
            {
                "description": "The immutable target contains the settled value.",
                "supplemental_evaluation": None,
                "tool_views": [],
            },
        ),
    )
    assert wait_for_response(client, "req-delayed-submit")["result"] == {
        "published": True
    }
    client.finish(annotation_child)
    service.join(timeout=2.0)
    assert not service.is_alive()
    assert result == [0]
    assert (
        runtime.annotation.task(run_id, "c001", 1).state == "completed"
    )

    for session in sessions:
        close_candidate(client, session)
    closed = close_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert closed is not None and closed["status"] == "closed"


def test_runtime_owned_s0_k2_exact_snapshot_settlement_and_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    client.completion_unknown_once.update(
        {
            "fs.snapshot.create",
            "fs.branch.snapshot",
            "fs.run",
            "fs.snapshot.patch",
            "fs.apply",
        }
    )
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path.parent / (tmp_path.name + "-session-runtime"))
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], agent_harness="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    sessions = [bind_candidate(runtime, client, run_id, task.candidate_id) for task in tasks]
    assert len([op for op, _ in client.operations if op == "fs.snapshot.create"]) == 1
    spawns = [params for op, params in client.operations if op == "thinkthread.spawn"]
    assert len(spawns) == 2
    assert {item["fsSnapshotId"] for item in spawns} == {"fsnap-S0"}
    assert len({session.session_handle.external_id for session in sessions}) == 2
    assert len({session.session_handle.metadata["fs_branch_id"] for session in sessions}) == 2
    assert all(not task.workspace.exists() for task in tasks)
    for session in sessions:
        context = runtime.get_agent_context(session.agent_session_id)
        assert "workspace" not in context
        assert "workspace" not in context["candidate_task"]
        assert context["workspace_access"]["logical_root"] == "."
        assert context["workspace_access"]["native_cwd"] is None
        assert context["workspace_access"]["source_subdir"] == "project"
        assert verify_candidate(runtime, client, session).process_passed
    kick_thinkthread_annotation(root_dir=runtime.root_dir, run_id=run_id, client=client)
    branch_snapshots = [
        params for op, params in client.operations if op == "fs.branch.snapshot"
    ]
    assert len(branch_snapshots) == 2
    candidate_runs = [
        params
        for op, params in client.operations
        if op == "fs.run" and params["snapshotId"] != "fsnap-S0"
    ]
    assert {item["snapshotId"] for item in candidate_runs} == {"fsnap-2", "fsnap-3"}
    assert all(item["writes"] == "discard" for item in candidate_runs)

    records = runtime.candidate_creation.state.read_candidates(run_id)
    assert {record.iterations[0].artifact_ref.id for record in records} == {
        "fsnap-2",
        "fsnap-3",
    }
    assert all(record.iterations[0].process_passed for record in records)
    for record in records:
        task = runtime.annotation.task(run_id, record.candidate_id, 1)
        assert task is not None
        assert task.attempt_artifact_ref == record.iterations[0].artifact_ref
        assert task.artifact_views[0].artifact_ref == record.iterations[0].artifact_ref

    annotation = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert annotation is not None
    annotation_child_id = str(annotation["thinkthread_id"])
    client.enqueue(
        annotation_child_id,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "registration",
            "registration_nonce": annotation["registration_nonce"],
        },
    )
    client.enqueue(
        annotation_child_id,
        annotation_request(
            annotation,
            "req-claim-before-selection",
            "goal_plus_thinkthread_annotation_next",
            {"run_id": run_id},
        ),
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    claimed_context = response_for(client, "req-claim-before-selection")["result"]["task"]
    claimed_task = runtime.annotation.task(
        run_id,
        claimed_context["candidate_id"],
        claimed_context["iteration"],
    )
    assert claimed_task is not None
    assert claimed_task.attempts == 1

    for session in sessions:
        close_candidate(client, session)
    selection = runtime.select(run_id)
    assert selection["selected_candidate_id"] == "c002"
    queue = runtime.annotation.queue(run_id)
    assert queue.state == "closed"
    assert queue.generation == 2
    assert (
        AnnotationHostService(runtime.root_dir).finish_annotation_task(
            claimed_task,
            result=EvidenceAnnotationResult(
                description="Late annotation after selection.",
                supplemental_evaluation=None,
                comparison_basis=list(claimed_task.comparison_basis),
                tool_views=[],
                usage={},
            ),
        )
        is False
    )
    assert all(
        runtime.annotation.task(run_id, record.candidate_id, 1).state
        == "cancelled"
        for record in records
    )
    assert not any(op == "fs.replace" for op, _ in client.operations)
    annotation = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert annotation is not None
    assert annotation["status"] == "running"
    settled = {
        path: path.read_bytes()
        for path in runtime.search_run.state.run_dir(run_id).rglob("*.json")
    }
    monkeypatch.setattr("goal_plus.thinkthread_annotation.AgentPosixSdkClient", lambda: client)
    client.completion_unknown_before_once.add("fs.branch.remove")
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        kick_pi_internal_agents(runtime.root_dir, run_id)
    incomplete = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert incomplete["status"] == "needs_recovery"
    assert incomplete["thinkthread_id"] == annotation_child_id
    assert all(path.read_bytes() == value for path, value in settled.items())
    assert kick_pi_internal_agents(runtime.root_dir, run_id) == {
        "annotation_started": False, "verifier_started": False,
    }
    assert all(path.read_bytes() == value for path, value in settled.items())
    annotation = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert annotation["status"] == "closed"
    assert client.children == []

    manifest = runtime.promote(run_id, "c002")
    prepared = runtime.promotion_record(run_id)
    assert manifest.is_file()
    assert prepared.state == "prepared"
    assert prepared.publication_artifact_ref is not None
    assert prepared.publication_artifact_ref.id == "fsnap-P"
    apply_intent = next(
        item
        for item in runtime.search_run.state.read_run(run_id).provider_mutations
        if item.operation == "apply"
    )
    assert apply_intent.state == "prepared"

    applied = runtime.apply_promotion(run_id)

    assert applied.state == "applied"
    persisted = runtime.search_run.state.read_run(run_id)
    assert persisted.search_result == "passed"
    assert persisted.publication_outcome is not None
    assert persisted.publication_outcome.state == "applied"
    assert persisted.publication_outcome.apply_outcome == "applied"
    assert persisted.publication_outcome.notification_state == "pending"
    assert len([op for op, _ in client.operations if op == "fs.apply"]) == 1
    assert not any(op == "fs.replace" for op, _ in client.operations)
    snapshot = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    assert snapshot["run"]["search_result"] == "passed"
    assert snapshot["run"]["publication_result"] == "applied"
    assert snapshot["search_tasks"][0]["search_result"] == "passed"
    assert snapshot["search_tasks"][0]["publication_result"] == "applied"
    pending_cleanup = snapshot["run"]["host_cleanup"]
    assert pending_cleanup["state"] == "pending"
    # This provider fixture supplies no host scope resource observations.
    assert pending_cleanup["candidate_sessions"]["complete"] is False
    assert pending_cleanup["annotation"]["state"] == "closed"
    assert pending_cleanup["annotation"]["service_complete"] is True
    assert pending_cleanup["annotation"]["complete"] is True
    assert pending_cleanup["snapshots"]["owned_total"] == 4
    assert pending_cleanup["snapshots"]["state"] == "pending"
    assert pending_cleanup["final_artifacts"]["complete"] is False
    cleanup_json = json.dumps(pending_cleanup, sort_keys=True)
    assert "host-pools" not in cleanup_json
    assert str(runtime.root_dir) not in cleanup_json
    assert snapshot["search_tasks"][0]["host_cleanup"] == pending_cleanup
    client.completion_unknown_once.add("fs.snapshot.remove")
    goals = FileGoalPlusRuntime(runtime.root_dir)
    goal_id = goals.authorization.run_owner(run_id)
    goals.record_search_result(goal_id, run_id, selected_candidate_id="c002", promotion_artifact_path=str(manifest))
    goals.set_status(goal_id, "complete")
    report = FileSearchRuntime(runtime.root_dir).report(run_id)
    report_text = report.read_text(encoding="utf-8")
    html_text = report.with_suffix(".html").read_text(encoding="utf-8")
    assert "- Search result: `passed`" in report_text
    assert "- Publication result: `applied`" in report_text
    assert "Search result" in html_text
    assert "Publication result" in html_text
    assert "Provider Artifact Lifecycle" in report_text
    assert "Provider Artifact Lifecycle" in html_text
    assert client.snapshots == set()
    cleanup = runtime.search_run.state.read_run(run_id).provider_cleanup
    assert {item.resource_id for item in cleanup} == {
        "fsnap-S0",
        "fsnap-2",
        "fsnap-3",
        "fsnap-P",
    }
    assert {item.state for item in cleanup} == {"removed"}
    remove_mutations = [
        item
        for item in runtime.search_run.state.read_run(run_id).provider_mutations
        if item.operation == "snapshot_remove"
    ]
    assert len(remove_mutations) == 4
    assert {item.state for item in remove_mutations} == {"closed"}
    assert len(
        [operation for operation, _params in client.operations if operation == "fs.snapshot.remove"]
    ) == 4
    evidence = runtime.get_global_evidence(sessions[0].agent_session_id)
    assert {item["artifact_availability"] for item in evidence} == {"removed"}
    assert all(item["reference_available"] is False for item in evidence)
    monitor = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    assert {
        item["artifact_availability"]
        for item in monitor["run"]["artifact_availability"]
    } == {"removed"}
    assert {
        item["artifact_ref"]["id"]
        for item in monitor["run"]["artifact_availability"]
    } == {"fsnap-S0", "fsnap-2", "fsnap-3", "fsnap-P"}
    completed_cleanup = monitor["run"]["host_cleanup"]
    assert completed_cleanup["state"] == "pending"
    assert completed_cleanup["candidate_sessions"]["complete"] is False
    assert completed_cleanup["annotation"]["complete"] is True
    assert completed_cleanup["snapshots"]["states"] == {"removed": 4}
    assert completed_cleanup["snapshots"]["complete"] is True
    assert completed_cleanup["final_artifacts"]["complete"] is True
    assert completed_cleanup["blockers"] == ["candidate_sessions:not_closed"]

    removed_task = runtime.annotation.task(run_id, "c001", 1)
    assert removed_task is not None
    with pytest.raises(RuntimeError, match="removed after terminal cleanup"):
        runtime.annotation_context.open_views(removed_task)

    remove_call_count = len(
        [operation for operation, _params in client.operations if operation == "fs.snapshot.remove"]
    )
    regenerated = FileSearchRuntime(runtime.root_dir).report(run_id)
    assert regenerated.is_file()
    assert len(
        [operation for operation, _params in client.operations if operation == "fs.snapshot.remove"]
    ) == remove_call_count














def test_annotation_spawn_wake_and_close_recover_without_duplicate_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path.parent / (tmp_path.name + "-session-runtime"))
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], agent_harness="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    tasks = runtime.start_batch(
        run_id,
        runtime.plan_next(run_id, requested_k=2).plan_id,
    )
    sessions = [bind_candidate(runtime, client, run_id, task.candidate_id) for task in tasks]
    verify_candidate(runtime, client, sessions[0])
    client.completion_unknown_once.add("thinkthread.spawn")
    kick_thinkthread_annotation(root_dir=runtime.root_dir, run_id=run_id, client=client)
    recovered_at_boundary = service_thinkthread_annotation(root_dir=runtime.root_dir, run_id=run_id, client=client)
    assert recovered_at_boundary is not None
    assert recovered_at_boundary["status"] == "running"
    assert (
        recovered_at_boundary["spawn_intent"]["state"]
        == "bound_after_recovery"
    )
    annotation_spawn_count = len(
        [operation for operation, _params in client.operations if operation == "thinkthread.spawn"]
    )

    recovered_spawn = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert recovered_spawn is not None
    assert recovered_spawn["status"] == "running"
    assert recovered_spawn["spawn_intent"]["state"] == "bound_after_recovery"
    assert len(
        [operation for operation, _params in client.operations if operation == "thinkthread.spawn"]
    ) == annotation_spawn_count

    first_task = runtime.annotation.task(
        run_id,
        tasks[0].candidate_id,
        1,
    )
    assert first_task is not None
    assert AnnotationHostService(runtime.root_dir).finish_annotation_task(
        first_task,
        result=EvidenceAnnotationResult(
            description="First candidate changed the program value.",
            supplemental_evaluation=None,
            comparison_basis=list(first_task.comparison_basis),
            tool_views=[],
            usage={},
        ),
    )
    annotation_child = str(recovered_spawn["thinkthread_id"])
    client.finish(annotation_child)
    client.child(annotation_child)["executionState"] = "running"
    ready = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert ready is not None and ready["status"] == "ready"

    client.completion_unknown_once.add("message.send")
    verify_candidate(runtime, client, sessions[1])
    kick_thinkthread_annotation(root_dir=runtime.root_dir, run_id=run_id, client=client)
    recovered_wake_at_boundary = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,
    )
    assert recovered_wake_at_boundary is not None
    assert recovered_wake_at_boundary["status"] == "running"
    assert (
        recovered_wake_at_boundary["wake_intent"]["state"]
        == "acknowledged_after_recovery"
    )
    wake_count = len([item for item in client.sent if item.get("wake") is True])

    recovered_wake = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert recovered_wake is not None and recovered_wake["status"] == "running"
    assert recovered_wake["wake_intent"]["state"] == "acknowledged_after_recovery"
    assert len([item for item in client.sent if item.get("wake") is True]) == wake_count

    client.enqueue(
        annotation_child,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "dispatch_ack",
            "dispatch_nonce": recovered_spawn["dispatch_nonce"],
        },
    )
    after_stale_ack = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert after_stale_ack is not None
    assert after_stale_ack["status"] == "running"

    for session in sessions:
        close_candidate(client, session)
    client.completion_unknown_before_once.add("fs.branch.remove")
    unresolved_close = close_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert unresolved_close is not None
    assert unresolved_close["status"] == "needs_recovery"
    still_fenced = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert still_fenced is not None and still_fenced["status"] == "needs_recovery"

    client.completion_unknown_once.add("thinkthread.destroy")
    closed = close_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert closed is not None and closed["status"] == "closed"
    assert closed["close_intent"]["state"] == "closed"


def test_retained_annotation_child_uses_full_snapshot_views_and_structured_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED", "1")
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path.parent / (tmp_path.name + "-session-runtime"))
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], agent_harness="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    runtime.start_batch(run_id, plan.plan_id)
    sessions = [bind_candidate(runtime, client, run_id, candidate_id)
                for candidate_id in [record.candidate_id for record in runtime.candidate_creation.state.read_candidates(run_id)]]
    for session in sessions:
        verify_candidate(runtime, client, session)
    kick_thinkthread_annotation(root_dir=runtime.root_dir, run_id=run_id, client=client)

    annotation_mutations_before = sum(
        operation
        in {
            "fs.run",
            "workflow.fs.snapshot.patchBytes",
            "fs.snapshot.remove",
        }
        for operation, _ in client.operations
    )

    original_snapshot_diff = ThinkThreadSnapshotReader.diff

    def truncated_snapshot_diff(*_args: Any, **_kwargs: Any) -> tuple[str, bool]:
        return "bounded diff payload that must not be inlined", True

    monkeypatch.setattr(ThinkThreadSnapshotReader, "diff", truncated_snapshot_diff)

    state = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert state is not None
    child_id = str(state["thinkthread_id"])
    client.enqueue(
        child_id,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "registration",
            "registration_nonce": state["registration_nonce"],
        },
    )
    client.enqueue(
        child_id,
        annotation_request(
            state,
            "req-next-1",
            "goal_plus_thinkthread_annotation_next",
            {"run_id": run_id},
        ),
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    first_context = response_for(client, "req-next-1")["result"]["task"]
    assert first_context["artifact_views"][0]["role"] == "target"
    assert first_context["actual_diff"] == ""
    assert first_context["actual_diff_omitted"]
    assert first_context["candidate_diff"] == ""
    assert first_context["candidate_diff_omitted"]
    first_task = runtime.annotation.task(run_id, "c001", 1)
    assert first_task is not None
    client.enqueue(
        child_id,
        annotation_request(
            state,
            "req-submit-1",
            "goal_plus_thinkthread_annotation_submit",
                {
                    "description": "First snapshot is a valid isolated implementation.",
                    "supplemental_evaluation": {
                        "summary": "The first implementation passes its exact verifier.",
                        "dimensions": [
                            {
                                "name": "correctness",
                                "finding": "The immutable target snapshot passed.",
                                "confidence": "high",
                                "evidence": ["settled process-verifier result"],
                            }
                        ],
                        "comparisons": [],
                        "limitations": [],
                    },
                "tool_views": [],
            },
        ),
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert response_for(client, "req-submit-1")["result"] == {"published": True}

    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert current is not None
    client.enqueue(
        child_id,
        annotation_request(
            current,
            "req-next-2",
            "goal_plus_thinkthread_annotation_next",
            {"run_id": run_id},
        ),
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    second_context = response_for(client, "req-next-2")["result"]["task"]
    assert second_context["actual_diff"] == ""
    assert second_context["actual_diff_omitted"]
    assert second_context["candidate_diff"] == ""
    assert second_context["candidate_diff_omitted"]
    assert second_context["peer_evidence"][0]["candidate_diff"] is None
    assert second_context["peer_evidence"][0]["diff_omitted"]
    views = second_context["artifact_views"]
    assert [item["role"] for item in views] == ["target", "peer"]
    target_id = views[0]["view_id"]
    peer_id = views[1]["view_id"]
    monkeypatch.setattr(ThinkThreadSnapshotReader, "diff", original_snapshot_diff)

    for request_id, tool, params in (
        (
            "req-list",
            "goal_plus_thinkthread_artifact_view_list",
            {"view_id": target_id, "path": "project"},
        ),
        (
            "req-stat",
            "goal_plus_thinkthread_artifact_view_stat",
            {"view_id": target_id, "path": "project/initial_program.py"},
        ),
        (
            "req-read",
            "goal_plus_thinkthread_artifact_view_pread",
            {
                "view_id": target_id,
                "path": "project/initial_program.py",
                "offset": 0,
                "length": 64 * 1024,
            },
        ),
        (
            "req-diff",
            "goal_plus_thinkthread_artifact_view_diff",
            {"base_view_id": peer_id, "target_view_id": target_id},
        ),
    ):
        current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
        assert current is not None
        client.enqueue(child_id, annotation_request(current, request_id, tool, params))
        service_thinkthread_annotation(
            root_dir=runtime.root_dir,
            run_id=run_id,
            client=client,  # type: ignore[arg-type]
        )
        assert response_for(client, request_id)["ok"] is True
    read_result = response_for(client, "req-read")["result"]
    assert base64.b64decode(read_result["dataBase64"]) == b"VALUE = 3\n"

    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert current is not None
    second_submit_request = annotation_request(
        current,
        "req-submit-2",
        "goal_plus_thinkthread_annotation_submit",
        {
            "description": "Second snapshot improves the target and differs from its peer.",
            "supplemental_evaluation": {
                "summary": "The second exact snapshot scores higher than its peer.",
                "dimensions": [
                    {
                        "name": "relative quality",
                        "finding": "The target has the stronger verifier result.",
                        "confidence": "high",
                        "evidence": ["target and peer snapshot evidence"],
                    }
                ],
                "comparisons": [
                    {
                        **second_context["comparison_basis"][0],
                        "relation": "different",
                        "rationale": "The target snapshot has a higher exact score.",
                        "evidence": ["immutable target and peer views"],
                    }
                ],
                "limitations": [],
            },
            "tool_views": [],
        },
    )
    client.enqueue(
        child_id,
        second_submit_request,
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert response_for(client, "req-submit-2")["result"] == {"published": True}
    assert runtime.annotation.task(run_id, "c001", 1).state == "completed"
    assert runtime.annotation.task(run_id, "c002", 1).state == "completed"
    assert len([op for op, _ in client.operations if op == "thinkthread.spawn"]) == 3
    assert sum(
        operation
        in {
            "fs.run",
            "workflow.fs.snapshot.patchBytes",
            "fs.snapshot.remove",
        }
        for operation, _ in client.operations
    ) == annotation_mutations_before

    response_chunks_before = len(
        [
            payload
            for item in client.sent
            if isinstance(item.get("text"), str)
            and (payload := json.loads(item["text"])).get("request_id") == "req-submit-2"
        ]
    )
    client.enqueue(child_id, second_submit_request)
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    response_chunks = [
        payload
        for item in client.sent
        if isinstance(item.get("text"), str)
        and (payload := json.loads(item["text"])).get("request_id") == "req-submit-2"
    ]
    assert len(response_chunks) == response_chunks_before * 2
    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert current is not None
    client.enqueue(
        child_id,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "response_ack",
            "request_id": "req-submit-2",
            "response_sha256": response_chunks[0]["response_sha256"],
        },
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert "req-submit-2" not in annotation_snapshot(
        root_dir=runtime.root_dir,
        run_id=run_id,
    )["pending_responses"]

    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    truncated = annotation_request(
        current,
        "req-truncated",
        "goal_plus_thinkthread_annotation_next",
        {"run_id": run_id},
    )
    client.enqueue(child_id, truncated)
    client.messages[child_id][-1]["truncated"] = True
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert not any(
        isinstance(item.get("text"), str)
        and json.loads(item["text"]).get("request_id") == "req-truncated"
        for item in client.sent
    )

    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    malformed = annotation_request(
        current,
        "req-bad-hash",
        "goal_plus_thinkthread_annotation_next",
        {"run_id": run_id},
    )
    malformed["content_sha256"] = "0" * 64
    client.enqueue(child_id, malformed)
    with pytest.raises(ValueError, match="content_sha256 mismatch"):
        service_thinkthread_annotation(
            root_dir=runtime.root_dir,
            run_id=run_id,
            client=client,  # type: ignore[arg-type]
        )

    for session in sessions:
        close_candidate(client, session)
    runtime.select(run_id)
    monkeypatch.setattr("goal_plus.thinkthread_annotation.AgentPosixSdkClient", lambda: client)
    kick_pi_internal_agents(runtime.root_dir, run_id)
    assert annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)["status"] == "closed"
