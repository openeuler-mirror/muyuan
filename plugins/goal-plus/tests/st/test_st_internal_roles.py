"""Shared role acceptance over settled Circle artifacts on both native hosts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from goal_plus.llm_verifier import LlmVerifier
from goal_plus.application.verifier import VerifierWorkflow
from goal_plus.infrastructure.state.verifier import FileVerifierState
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.evidence_annotator import HostEvidenceAnnotator
from goal_plus.host_scope import (
    close_execution_scope,
    ensure_execution_scope,
    execution_scope_snapshot,
)
from goal_plus.models import CandidateProposal, SearchSpec
from goal_plus.runtime import FileSearchRuntime
from tests.st.hosts import st_model_for_host

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.st
@pytest.mark.parametrize("agent_harness", [
    pytest.param("codex", marks=pytest.mark.st_codex),
    pytest.param("pi", marks=pytest.mark.st_pi_rpc),
])
def test_circle_annotation_verifier(
    tmp_path: Path, st_log_dir: Path, monkeypatch: pytest.MonkeyPatch,
    agent_harness: str, isolated_pi_config: Path, request,
) -> None:
    role_host = "codex" if agent_harness == "codex" else "pi"
    model = st_model_for_host(role_host)
    if agent_harness == "codex":
        request.getfixturevalue("installed_codex_config")
    else:
        monkeypatch.setenv("GOAL_PLUS_PI_DEV_ROOT", str(ROOT))
        monkeypatch.setenv("GOAL_PLUS_PYTHON", str(ROOT / ".goal-plus-dev/venv/bin/python"))
        if not model:
            settings = json.loads((isolated_pi_config / "settings.json").read_text())
            model = settings["defaultProvider"] + "/" + settings["defaultModel"]
    thinking = "low"
    timeout = int(os.environ.get("ST_ROLE_TIMEOUT", "180"))
    monkeypatch.setenv("GOAL_PLUS_INTERNAL_AGENT_HARNESS", role_host)
    if agent_harness == "codex":
        monkeypatch.setenv("GOAL_PLUS_CODEX_MODEL", model)
        monkeypatch.setenv("GOAL_PLUS_CODEX_REASONING_EFFORT", thinking)
    source = tmp_path / "source"
    example = ROOT / "examples" / "circle-packing-git-apply"
    subprocess.run(
        [sys.executable, str(example / "prepare_workspace.py"),
         "--destination", str(source)],
        check=True, capture_output=True, text=True,
    )
    data = json.loads((example / "search-spec.json").read_text())
    data["source_path"] = str(source)
    data["budget"] = {"max_parallel": 1, "max_candidates": 1}
    profile = {
        "model": model, "reasoning_effort": thinking,
        "timeout_seconds": timeout,
    }
    data["strategy"].update({
        "initial_context": "fresh", "inner_agent": "common",
        "evidence_annotator": profile,
        "verifier": {"scores": ["progress_reward", "allocation_utility"]},
        "adaptive_search": {"allocation": {"policy": "swe_search_uct/v1"}},
    })
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [source / ".goal-plus-verifiers" / "circle_packing_score.py"], agent_harness=agent_harness,
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    candidate = runtime.start_batch(run_id, plan.plan_id, proposals=[CandidateProposal(
        intent="Evaluate one small Circle ring adjustment",
        hypothesis="Moving the inner ring changes the valid radius sum.",
        instructions=["Adjust the inner ring radius from 0.28 to 0.29 and verify."],
    )])[0]
    # A deterministic input change isolates role execution from candidate inference.
    program = Path(candidate.workspace) / "initial_program.py"
    program.write_text(program.read_text().replace("0.28", "0.29"))
    report = runtime.run_verifier(run_id, candidate.candidate_id)
    assert report.process_passed
    before = runtime.candidate_creation.state.read_candidate(run_id, candidate.candidate_id)
    hard_evidence = [
        item.model_dump(mode="json") for item in before.iterations
    ]
    scope = ensure_execution_scope(
        root_dir=runtime.root_dir, agent_harness=agent_harness, owner_session_id=tmp_path.name,
         run_id=run_id,
        native_agent_config={"model": model, "reasoning_effort": thinking} if agent_harness == "codex"
        else {"model_pattern": model, "thinking_level": thinking},
    )
    print(f"Circle {agent_harness} role ST: {st_log_dir}; run={run_id}; model={model}", flush=True)
    (st_log_dir / "build.json").write_text(json.dumps({
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "checkout": str(ROOT), "root_dir": str(runtime.root_dir), "run_id": run_id,
        "agent_harness": agent_harness, "model": model, "reasoning_effort": thinking,
    }, indent=2))
    try:
        runtime.annotation_admission.ensure(run_id)
        task = runtime.annotation.claim(run_id, candidate.candidate_id, 1)
        assert task is not None
        context = runtime.annotation_context.build(run_id, candidate.candidate_id, 1)
        context["_annotation_monitor_path"] = str(
            runtime.root_dir / task.attempt_history[-1]["monitor_path"]
        )
        try:
            result = HostEvidenceAnnotator().annotate(context)
        except Exception as exc:
            runtime.annotation.complete(task, error=exc)
            raise
        assert runtime.annotation.complete(task, result=result).accepted
        annotated = runtime.annotation.task(run_id, candidate.candidate_id, 1)
        assert annotated is not None and annotated.state == "completed"
        assert annotated.view is not None and annotated.view.description
        assert result.usage.get("output_tokens", 0) > 0
        (st_log_dir / "annotation.json").write_text(annotated.model_dump_json(indent=2))
        print("Annotation completed through real structured inference", flush=True)

        workflow = VerifierWorkflow(FileVerifierState(runtime.root_dir), SystemClock())
        claimed = workflow.claim_task("internal-role-st", timeout, run_id=run_id)
        assert claimed is not None
        result, invocation, usage = LlmVerifier(runtime.root_dir).execute(claimed)
        finished = workflow.complete_task(
            claimed.task_id, result, invocation, usage=usage,
            worker_id=claimed.lease.worker_id, attempt=claimed.attempts,
        )
        assert finished.invocation.agent_harness == role_host
        assert finished.invocation.model == model
        assert finished.scores is not None
        assert usage.get("output_tokens", 0) > 0
        (st_log_dir / "verifier.json").write_text(finished.model_dump_json(indent=2))
        print("Verifier completed through real structured inference", flush=True)

        after = runtime.candidate_creation.state.read_candidate(run_id, candidate.candidate_id)
        assert [
            item.model_dump(mode="json") for item in after.iterations
        ] == hard_evidence
        assert runtime.status(run_id).best_score == report.aggregate_score
        resources = execution_scope_snapshot(
            root_dir=runtime.root_dir, scope_id=scope["scope_id"],
        )["processes"]
        assert {item["label"] for item in resources} == {"annotation", "llm-verifier"}
        assert all(item["status"] == "finished" for item in resources)
    finally:
        closed = close_execution_scope(
            root_dir=runtime.root_dir, scope_id=scope["scope_id"], reason="circle_role_st",
            timeout_seconds=15,
        )
        assert closed["state"] == "closed", closed
        (st_log_dir / "cleanup.json").write_text(json.dumps(closed, indent=2))
