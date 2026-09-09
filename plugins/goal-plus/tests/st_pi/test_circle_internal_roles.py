"""Real Pi role adapters over settled Circle artifacts, without a native Main."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from goal_plus.adaptive_search.scheduler import SchedulerManager, SearchScheduler
from goal_plus.domain.adaptive.scheduler import SchedulerError
from goal_plus.evidence_annotator import PiEvidenceAnnotator
from goal_plus.host_scope import (
    close_execution_scope,
    ensure_execution_scope,
    execution_scope_snapshot,
)
from goal_plus.models import CandidateProposal, SearchSpec
from goal_plus.runtime import FileSearchRuntime

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.st_pi
def test_circle_annotation_scheduler(
    st_pi_run_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = os.environ.get("ST_PI_MODEL")
    if not model:
        pytest.skip("ST_PI_MODEL is required to pin both internal role models")
    thinking = os.environ.get("ST_PI_THINKING", "low")
    timeout = int(os.environ.get("ST_PI_ROLE_TIMEOUT", "180"))
    monkeypatch.setenv("GOAL_PLUS_INTERNAL_AGENT_HOST", "pi-rpc")
    source = st_pi_run_root / "source"
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
        "orchestration_mode": "adaptive_search", "inner_agent": "common",
        "evidence_annotator": profile,
        "search_scheduler": {
            **profile,
            "reward": "evidence_llm_value/v3 @prompt Evaluate the settled Circle change.",
            "allocation": "value_guided_replace/v2",
        },
    })
    runtime = FileSearchRuntime(st_pi_run_root / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [source / ".goal-plus-verifiers" / "circle_packing_score.py"], native_host="pi",
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
    scheduler_projection = {"schedule_task_id", "scheduler_status"}
    hard_evidence = [
        item.model_dump(mode="json", exclude=scheduler_projection) for item in before.iterations
    ]
    scope = ensure_execution_scope(
        root_dir=runtime.root_dir, host="pi", owner_session_id=st_pi_run_root.name,
        owner_pid=os.getpid(), run_id=run_id, start_guardian=False,
        native_agent_config={"model_pattern": model, "thinking_level": thinking},
    )
    print(f"Circle role ST: {st_pi_run_root}; run={run_id}; model={model}", flush=True)
    try:
        task = runtime.annotation.claim(run_id, candidate.candidate_id, 1)
        assert task is not None
        context = runtime.annotation_context.build(run_id, candidate.candidate_id, 1)
        context["_annotation_monitor_path"] = str(
            runtime.root_dir / task.attempt_history[-1]["monitor_path"]
        )
        try:
            result = PiEvidenceAnnotator().annotate(context)
        except Exception as exc:
            runtime.annotation.complete(task, error=exc)
            raise
        assert runtime.annotation.complete(task, result=result).accepted
        annotated = runtime.annotation.task(run_id, candidate.candidate_id, 1)
        assert annotated is not None and annotated.state == "completed"
        assert annotated.view is not None and annotated.view.description
        assert result.usage.get("output_tokens", 0) > 0
        print("Annotation completed through real Pi output tool", flush=True)

        manager = SchedulerManager(runtime)
        manager.ensure_tasks(run_id)
        scheduled = manager.workflow.claim(run_id, candidate.candidate_id, 1)
        assert scheduled is not None
        context = manager.context.build(run_id, candidate.candidate_id, 1)
        context["_execution"]["attempt"] = scheduled.attempts
        context["_execution"]["monitor_path"] = str(
            runtime.root_dir / scheduled.attempt_history[-1]["monitor_path"]
        )
        try:
            result = SearchScheduler().schedule(context)
        except SchedulerError as exc:
            manager.workflow.complete(scheduled, error=exc)
            raise
        assert manager.workflow.complete(scheduled, result=result)
        manager.consume_tasks(run_id, candidate.candidate_id, through_iteration=1)
        finished = manager.load_task(run_id, candidate.candidate_id, 1)
        assert finished is not None and finished.state == "completed"
        assert finished.invocation is not None
        assert finished.invocation.host == "pi-rpc"
        assert finished.invocation.model == model
        assert manager.list_rewards(run_id, candidate.candidate_id)
        assert result.usage.get("output_tokens", 0) > 0
        print("Scheduler completed and reward consumed", flush=True)

        after = runtime.candidate_creation.state.read_candidate(run_id, candidate.candidate_id)
        assert [
            item.model_dump(mode="json", exclude=scheduler_projection) for item in after.iterations
        ] == hard_evidence
        assert after.iterations[0].schedule_task_id == finished.task_id
        assert after.iterations[0].scheduler_status == "completed"
        assert runtime.status(run_id).best_score == report.aggregate_score
        resources = execution_scope_snapshot(
            root_dir=runtime.root_dir, scope_id=scope["scope_id"],
        )["processes"]
        assert {item["label"] for item in resources} == {"annotation", "scheduler"}
        assert all(item["status"] == "finished" for item in resources)
        assert all(not Path("/proc", str(item["pid"])).exists() for item in resources)
    finally:
        closed = close_execution_scope(
            root_dir=runtime.root_dir, scope_id=scope["scope_id"], reason="circle_role_st",
            timeout_seconds=15,
        )
        assert closed["state"] == "closed", closed
