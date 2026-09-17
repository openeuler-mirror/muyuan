from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest

from goal_plus.domain.agents import SelectedModel
from goal_plus.domain.search import CandidateTask, SearchPlan, CandidateWorkOrder
from goal_plus.domain.promotion import PromotionRecord
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.ports.session import SessionCallSnapshot, SessionInvocationResult
from goal_plus.projections.search_tools import (
    candidate_task_receipt, frozen_spec_receipt, promotion_apply_receipt, search_plan_receipt,
)
from goal_plus.projections.session_tools import invocation_receipt, session_call_receipt
from goal_plus.tools import SearchTools
from tests.test_tools import frozen_spec


@pytest.mark.parametrize("fits", [True, False, None])
def test_freeze_receipt_only_expands_capacity_warning(fits):
    frozen = frozen_spec()
    frozen.workspace_capacity_estimate.fits_available_space = fits
    before = frozen.model_dump(mode="json")
    receipt = frozen_spec_receipt(frozen)
    assert receipt["state"] == "frozen"
    if fits is False:
        assert receipt["warnings"] == [{
            "code": "WorkspaceCapacityExceeded", "required_bytes": 100, "available_bytes": 1000,
        }]
    else:
        assert "warnings" not in receipt
    assert set(receipt) - {"warnings"} == {
        "frozen_spec_id", "state", "metric_name", "baseline", "promotion_verifiers",
    }
    assert frozen.model_dump(mode="json") == before


def test_plan_and_batch_receipts_preserve_allocated_ids_models_and_replay_contract():
    selected = SelectedModel(slot=1, model="resolved-model", provider="provider", reasoning_effort="high")
    plan = SearchPlan(
        run_id="run_1", plan_id="plan_1", status="started", strategy=frozen_spec().spec.strategy,
        requested_k=2, planned_k=1, remaining_budget=1, created_at="now",
        selected_models=[selected], started_candidate_ids=["c001"],
        work_orders=[CandidateWorkOrder(slot=1, intent="fixed task", instructions=["check the edge case"])],
        worker_policy={"private": "host-policy"}, strategy_trace={"private": "planning-diagnostics"},
    )
    task = CandidateTask(
        run_id="run_1", candidate_id="c001", plan_id="plan_1", hypothesis="fixed task",
        workspace=Path("/private/workspace"), allowed_files=["program.py"], denied_files=["verify.py"],
        instructions=["check the edge case"], selected_model=selected,
        workspace_provider_handle={"private": "handle"},
    )
    before = task.model_dump(mode="json")
    runtime = Mock()
    runtime.plan_next.return_value = plan
    runtime.start_batch.return_value = [task]
    tools = SearchTools(runtime)
    receipt = tools.goal_plus_search_plan_next("run_1", requested_k=2)
    assert receipt["planned_k"] == 1
    assert receipt["started_candidate_ids"] == ["c001"]
    assert receipt["work_orders"][0]["instructions"] == ["check the edge case"]
    assert receipt["selected_models"][0]["model"] == "resolved-model"
    assert "worker_policy" not in receipt and "strategy_trace" not in receipt
    batch = tools.goal_plus_search_start_batch("run_1", "plan_1")
    assert batch == [candidate_task_receipt(task)]
    assert batch[0]["selected_model"]["reasoning_effort"] == "high"
    assert "instructions" not in batch[0] and "workspace_provider_handle" not in batch[0]
    assert tools.goal_plus_search_start_batch("run_1", "plan_1") == batch
    assert task.model_dump(mode="json") == before


@pytest.mark.parametrize("state,error", [
    ("completed", None), ("failed", "VerifierTimeout"), ("interrupted", "user interrupted"),
])
def test_session_receipt_preserves_state_and_errors_without_echoing_output(state, error):
    result = SessionInvocationResult(
        "native-1", "invocation-1", state, '{"state":"verified","score_report":{"aggregate_score":1}}',
        agent_harness="pi", runtime_provider="thinkthread", error=error,
        usage={"tokens": 123},
    )
    snapshot = SessionCallSnapshot("call-1", "native-1", state, result)
    before = asdict(snapshot)
    receipt = session_call_receipt(snapshot)
    assert receipt["state"] == state
    assert receipt["invocation_id"] == "invocation-1"
    assert receipt["result_available"] is True
    assert "result" not in receipt and "text" not in receipt
    assert receipt.get("error") == error
    assert session_call_receipt(snapshot, detail=True) == before
    assert asdict(snapshot) == before
    assert invocation_receipt(result)["text"] == result.text
    assert "text" not in invocation_receipt(result, include_text=False)


def test_wait_running_and_controller_failure_receipts_are_unambiguous():
    assert session_call_receipt(SessionCallSnapshot("call-1", None, "running")) == {
        "call_id": "call-1", "state": "running",
    }
    assert session_call_receipt(SessionCallSnapshot("call-1", None, "failed", error="LostInvocation")) == {
        "call_id": "call-1", "state": "failed", "error": "LostInvocation",
    }


@pytest.mark.parametrize("state,error", [("applied", None), ("apply_failed", "source changed")])
def test_publication_receipt_keeps_changed_files_and_conflicts(state, error):
    artifact = ArtifactRef(kind="git_commit", id="commit-1", provider="git_worktree")
    promotion = PromotionRecord(
        promotion_id="promotion-1", run_id="run-1", candidate_id="c001", provider="git_worktree",
        strategy="squash_commit", state=state, base_artifact_ref=artifact, selected_artifact_ref=artifact,
        publication_artifact_ref=artifact, source_repository=Path("/source"), source_subdir="task",
        changed_files=["program.py"], created_at="now", error=error,
        provider_handle={"private": "provider-state"}, apply_result={"method": "provider-operation"},
    )
    before = promotion.model_dump(mode="json")
    receipt = promotion_apply_receipt(promotion)
    assert receipt["state"] == state
    assert receipt["changed_files"] == ["program.py"]
    assert receipt["source_path"] == "/source/task"
    assert receipt.get("error") == error
    assert "provider_handle" not in receipt and "apply_result" not in receipt
    assert promotion.model_dump(mode="json") == before
