"""Bounded real VLIW candidate turns without model-driven Main orchestration."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any

from goal_plus.models import CandidateProposal, SearchSpec
from goal_plus.runtime import FileSearchRuntime
from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient, validate_thinkthread_root
from goal_plus.thinkthread_annotation import (
    annotation_snapshot,
    close_thinkthread_annotation,
    service_thinkthread_annotation,
)
from goal_plus.thinkthread_pool import (
    close_pool,
    continue_thinkthread_search_pool,
    open_thinkthread_search_pool,
    wait_any,
)


EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "vliw-kernel-optimization"


def run_vliw_retained_turns(config: dict[str, Any]) -> dict[str, Any]:
    os.environ["GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED"] = "1"
    os.environ["GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED"] = "1"
    client = AgentPosixSdkClient()
    root = validate_thinkthread_root(client)
    workspace = Path(config["report_path"]).parent
    project = workspace / "workspace-template"
    shutil.copytree(
        EXAMPLE / "workspace-template", project,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    baseline_hash = hashlib.sha256((project / "solution.py").read_bytes()).hexdigest()
    payload = json.loads((EXAMPLE / "thinkthread-search-spec.json").read_text())
    payload["source_path"] = str(project)
    payload["budget"]["max_parallel"] = 1
    payload["root_hypotheses"] = ["Reuse input addresses across each item update."]
    runtime_root = Path(os.environ["GOAL_PLUS_ROOT"]) / "st-vliw"
    runtime = FileSearchRuntime(runtime_root)
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(payload),
        [path for path in project.rglob("*") if path.is_file() and path.name != "solution.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    runtime.start_batch(run_id, plan.plan_id, proposals=[CandidateProposal(
        intent="Complete two small correct VLIW optimizations across retained turns.",
        hypothesis="Reusing each input address eliminates one scalar add per item-round.",
        instructions=[
            "Edit only workspace-template/solution.py. Do not perform broad scheduling or vectorization.",
            "On the first turn allocate a dedicated tmp_val_addr scratch, compute the input-value address there before its load, and reuse it for the final value store, removing that store's redundant address add. Keep every other instruction unchanged.",
            "On the next retained wake do the analogous change for the input-index address using tmp_idx_addr; preserve the first turn's value-address optimization.",
            "Use the injected authoritative feedback and results.tsv to distinguish the two turns. Submit search_run_verifier once after the change and end the turn immediately after accepted. Do not wait to optimize anything else.",
        ],
    )])
    opened = open_thinkthread_search_pool(root_dir=runtime_root, run_id=run_id, client=client)
    pool_id = opened["pool_id"]
    original = opened["jobs"][0]
    turns = []
    try:
        for turn in (1, 2):
            if turn == 2:
                continued = continue_thinkthread_search_pool(
                    root_dir=runtime_root, pool_id=pool_id, candidate_id="c001", client=client,
                )
                job = continued["jobs"][0]
                for key in ("thinkthread_id", "agent_session_id", "fs_branch_id"):
                    assert job[key] == original[key], (key, original, job)
                assert job["dispatch_index"] == turn
                assert job["attempt_history"][0]["attempt_id"] == turns[0]["attempt_id"]
            waited = wait_any(root_dir=runtime_root, pool_id=pool_id, timeout_seconds=240, client=client)
            event = waited["event"]
            assert event is not None and event["kind"] == "candidate_ready", waited
            feedback = event["attempt_result"]
            record = runtime.candidate_creation.state.read_candidate(run_id, "c001")
            iteration = record.iterations[-1]
            assert len(record.iterations) == len(record.results_ledger) == turn
            assert record.inner_agent_state["result_count"] == turn
            assert iteration.agent_session_id == original["agent_session_id"]
            assert iteration.process_passed and iteration.disposition == "keep"
            assert iteration.score == 147734 - 4096 * turn
            assert feedback["score_report"]["verifier_results"][0]["metrics"]["snapshot_id"] == iteration.artifact_ref.id
            ledger_snapshot = feedback["workspace_artifact_after_attempt"]["id"]
            ledger = client.snapshot_read_file(ledger_snapshot, "workspace-template/results.tsv").decode()
            assert len(ledger.splitlines()) == turn + 1
            turns.append({
                "iteration": turn, "score": iteration.score,
                "attempt_id": feedback["attempt_id"],
                "artifact_id": iteration.artifact_ref.id,
                "ledger_rows": turn,
                "inner_git_head": record.inner_agent_state["attempt_git_heads"][str(turn)],
            })

        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            state = service_thinkthread_annotation(root_dir=runtime_root, run_id=run_id, client=client)
            tasks = [runtime.annotation.task(run_id, "c001", turn) for turn in (1, 2)]
            if all(task is not None and task.state == "completed" for task in tasks):
                break
            assert not any(task is not None and task.state == "terminal_error" for task in tasks), tasks
            assert state is None or state.get("status") not in {"failed", "needs_recovery"}, state
            time.sleep(0.5)
        else:
            raise TimeoutError(f"VLIW annotations incomplete: {annotation_snapshot(root_dir=runtime_root, run_id=run_id)}")
        for task in tasks:
            assert task.view is not None and task.view.supplemental_evaluation is not None
            assert task.attempt_artifact_ref.id == turns[task.iteration - 1]["artifact_id"]
        settled = list(state.get("settled_requests", {}).values())
        reads = [item for item in settled if item.get("tool") == "thinkthread_artifact_view_pread"]
        assert reads, "Annotator did not inspect immutable code"
        annotation_child = state["thinkthread_id"]
        selected = runtime.select(run_id)
        runtime.promote(run_id, "c001")
        report = runtime.report(run_id)
        promotion = runtime.promotion_record(run_id)
        assert promotion.promotion_mode == "artifact_only" and promotion.state == "prepared"
        assert promotion.applied_at is None
        assert report.is_file() and report.with_suffix(".html").is_file()
        assert hashlib.sha256((project / "solution.py").read_bytes()).hexdigest() == baseline_hash
    finally:
        closed = close_pool(root_dir=runtime_root, pool_id=pool_id, mode="interrupt", client=client)
        close_thinkthread_annotation(root_dir=runtime_root, run_id=run_id, client=client)
    assert not client.invoke("thinkthread.list").get("children"), closed
    runtime.report(run_id)
    cleanup = runtime.cleanup_thinkthread_snapshots(run_id)
    assert cleanup["state"] == "retained", cleanup
    cleanup_records = runtime.search_run.state.read_run(run_id).provider_cleanup
    assert all(item.state in {"removed", "retained"} for item in cleanup_records), cleanup_records
    assert all(item.pins == ["publication:prepared"] for item in cleanup_records if item.state == "retained")
    return {
        "schema_version": 1, "scenario": config["scenario"],
        "run_id": run_id, "root_id": root["thinkthreadId"],
        "model": config["model"], "thinking": config["thinking"],
        "candidate_id": "c001", "child_id": original["thinkthread_id"],
        "agent_session_id": original["agent_session_id"], "turns": turns,
        "annotation_child_id": annotation_child, "annotations_completed": len(tasks),
        "immutable_reads": len(reads), "selection": selected,
        "promotion_state": promotion.state, "promotion_mode": promotion.promotion_mode,
        "root_unchanged": True, "report_path": str(report),
        "snapshot_cleanup_state": cleanup["state"],
        "cleanup_states": [item["state"] for item in closed["cleanup"]],
    }
