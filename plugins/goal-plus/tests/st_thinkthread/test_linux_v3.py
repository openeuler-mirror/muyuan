from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from goal_plus.thinkthread_agent_posix import (
    AGENT_POSIX_CONTRACT_FINGERPRINT,
    AGENT_POSIX_CONTROL_PROTOCOL_VERSION,
)

pytestmark = [pytest.mark.st, pytest.mark.st_pi_thinkthread]


def test_pi_root_preflight_is_exact_and_starts_without_search(
    run_thinkthread_scenario: Callable[[str], dict[str, Any]],
) -> None:
    report = run_thinkthread_scenario("root_preflight")

    assert report["passed_test_ids"] == [
        "TT-LNX-BOOT-001",
        "TT-LNX-REGRESSION-001",
    ]
    assert report["environment"]["control_protocol"] == (
        AGENT_POSIX_CONTROL_PROTOCOL_VERSION
    )
    assert report["environment"]["contract_fingerprint"] == (
        AGENT_POSIX_CONTRACT_FINGERPRINT
    )
    assert report["root"]["parent_thinkthread_id"] is None
    assert report["root"]["filesystem"]["kind"] == "direct"
    assert report["root"]["filesystem"]["state"] == "attached"
    assert report["root"]["children_before_search"] == 0
    assert set(report["root"]["capabilities"]) == {
        "thinkthread.child",
        "thinkthread.fs",
        "thinkthread.fs.self",
        "thinkthread.message",
    }


def test_control_abi_and_single_pi_child_smoke(
    run_thinkthread_scenario: Callable[[str], dict[str, Any]],
) -> None:
    report = run_thinkthread_scenario("abi_single_child")

    assert set(report["passed_test_ids"]) == {
        "TT-LNX-CHILD-001",
        "TT-LNX-APPLY-001",
        "TT-LNX-CONFLICT-001",
        "TT-LNX-CONFLICT-002",
    }
    assert report["single_child"]["thinkthread_id"].startswith("tt-")
    assert report["single_child"]["fs_branch_id"].startswith("fsbranch-")
    assert report["snapshots"]["baseline"].startswith("fsnap-")
    assert report["snapshots"]["publication"].startswith("fsnap-")
    assert report["snapshots"]["run_snapshot"].startswith("fsnap-")
    assert len(set(report["request_ids"].values())) == 3
    assert set(report["abi"]["diff_paths"]) >= {
        "abi.txt",
        "created",
        "created/new.txt",
        "delete-me.txt",
    }


def test_runtime_k2_uses_private_children_and_exact_snapshot_evidence(
    run_thinkthread_scenario: Callable[[str], dict[str, Any]],
) -> None:
    report = run_thinkthread_scenario("runtime_k2")

    assert report["passed_test_ids"] == [
        "TT-LNX-CHILD-001",
        "TT-LNX-VERIFY-001",
    ]
    assert report["baseline_snapshot_id"].startswith("fsnap-")
    assert report["inner_agent"] == "common"
    assert report["inner_agent_binding"]["inner_agent_id"] == "common"
    assert report["inner_agent_binding"]["worker_host"] == "thinkthread"
    assert report["inner_agent_binding"]["workspace_ledger_projection"] == "none"
    assert report["event_candidates"] == ["c001", "c002"]
    assert report["root_solution"] == "0\n"
    assert len(report["candidates"]) == 2
    assert {item["score"] for item in report["candidates"].values()} == {1.0, 2.0}
    for item in report["candidates"].values():
        assert item["thinkthread_id"].startswith("tt-")
        assert item["agent_session_id"].startswith("agent_")
        assert item["fs_branch_id"].startswith("fsbranch-")
        assert item["artifact_ref"]["kind"] == "fs_snapshot"
        assert item["artifact_ref"]["provider"] == "thinkthread"
        assert item["verifier_snapshot_id"] == item["artifact_ref"]["id"]
    assert report["continuation"]["candidate_id"] == "c001"
    assert report["continuation"]["previous_attempt_id"] == (
        report["continuation"]["history_attempt_id"]
    )
    assert report["continuation"]["next_attempt_id"] != (
        report["continuation"]["previous_attempt_id"]
    )
    assert report["continuation"]["iteration_count"] == 2
    assert report["continuation"]["results_ledger_count"] == 2
    assert report["continuation"]["results_tsv"] is None
    assert report["cleanup_states"] == ["removed", "removed"]


def test_runtime_k2_autoresearch_uses_private_inner_git_and_outer_snapshot_evidence(
    run_thinkthread_scenario: Callable[[str], dict[str, Any]],
) -> None:
    report = run_thinkthread_scenario("runtime_k2_autoresearch")

    assert report["passed_test_ids"] == [
        "TT-LNX-CHILD-001",
        "TT-LNX-VERIFY-001",
        "TT-LNX-AUTORESEARCH-001",
    ]
    assert report["inner_agent"] == "autoresearch"
    assert report["inner_agent_binding"]["inner_agent_id"] == "autoresearch"
    assert report["inner_agent_binding"]["worker_host"] == "thinkthread"
    assert report["inner_agent_binding"]["prompt_asset"] == (
        "assets/pi/prompts/search-candidate-worker.md"
    )
    assert report["inner_agent_binding"]["workspace_ledger_projection"] == (
        "results_tsv"
    )
    assert report["autoresearch_prompt_loaded"] is True
    assert report["inner_git_verified"] is True
    assert report["artifact_view_private_paths_filtered"] is True
    assert report["event_candidates"] == ["c001", "c002"]
    assert {item["score"] for item in report["candidates"].values()} == {1.0, 2.0}
    for item in report["candidates"].values():
        assert item["artifact_ref"]["kind"] == "fs_snapshot"
        assert item["artifact_ref"]["provider"] == "thinkthread"
        assert item["verifier_snapshot_id"] == item["artifact_ref"]["id"]
    assert report["continuation"]["previous_attempt_id"] == (
        report["continuation"]["history_attempt_id"]
    )
    assert report["continuation"]["next_attempt_id"] != (
        report["continuation"]["previous_attempt_id"]
    )
    assert report["continuation"]["iteration_count"] == 2
    assert report["continuation"]["results_ledger_count"] == 2
    assert report["continuation"]["results_tsv"] == "results.tsv"
    assert report["continuation"]["score"] == 3.0
    assert report["continuation"]["disposition"] == "keep"
    assert report["root_solution"] == "0\n"
    assert report["cleanup_states"] == ["removed", "removed"]


def test_retained_annotation_child_uses_complete_snapshot_artifact_views(
    run_thinkthread_scenario: Callable[[str], dict[str, Any]],
) -> None:
    report = run_thinkthread_scenario("annotation_views")

    assert report["passed_test_ids"] == ["TT-LNX-ANNOTATE-001"]
    assert report["annotation_child_id"].startswith("tt-")
    assert report["peer_comparison_count"] == 1
    assert any(report["actual_diff_omitted"])
    assert report["tool_counts"]["thinkthread_artifact_view_list"] >= 1
    assert report["tool_counts"]["thinkthread_artifact_view_stat"] >= 1
    assert report["tool_counts"]["thinkthread_artifact_view_pread"] >= 2
    assert report["tool_counts"]["thinkthread_artifact_view_diff"] >= 1
    assert "thinkthread_artifact_view_run" not in report["tool_counts"]
    assert report["tool_counts"]["thinkthread_annotation_submit"] >= 2
    assert report["published_submit_count"] == 2
    assert report["rejected_submit_count"] == (
        report["tool_counts"]["thinkthread_annotation_submit"] - 2
    )
    assert report["cleanup_states"] == ["removed", "removed"]


def test_selection_cancels_claimed_annotation_and_rejects_late_result(
    run_thinkthread_scenario: Callable[[str], dict[str, Any]],
) -> None:
    report = run_thinkthread_scenario("annotation_cancel")

    assert report["passed_test_ids"] == ["TT-LNX-CANCEL-001"]
    assert report["annotation_child_id"].startswith("tt-")
    assert report["queue_generation_after"] == (
        report["queue_generation_before"] + 1
    )
    assert report["late_result_published"] is False
    assert report["cleanup_states"] == ["removed"]


def test_publication_conflict_recovery_and_cleanup_matrix(
    run_thinkthread_scenario: Callable[[str], dict[str, Any]],
) -> None:
    report = run_thinkthread_scenario("publication_recovery")

    assert set(report["passed_test_ids"]) == {
        "TT-LNX-APPLY-001",
        "TT-LNX-CONFLICT-001",
        "TT-LNX-CONFLICT-002",
        "TT-LNX-RECOVERY-001",
        "TT-LNX-RECOVERY-002",
        "TT-LNX-CLEANUP-001",
    }
    assert report["applied"]["state"] == "applied"
    assert report["applied"]["unrelated_content"] == "main unrelated edit\n"
    assert report["applied"]["cleanup"]["state"] == "complete"
    assert report["applied"]["cleanup"]["record_states"] == ["removed"]
    assert report["changed_conflict"]["state"] == "awaiting_main_integration"
    assert report["changed_conflict"]["cleanup"]["state"] == "retained"
    assert report["dependency_conflict"]["state"] == "awaiting_main_integration"
    assert report["response_loss"]["first_state"] == "recovering"
    assert report["response_loss"]["recovered_state"] == "applied"
    assert report["response_loss"]["apply_calls"] == 1
    assert report["needs_recovery"]["state"] == "recovering"
    assert report["needs_recovery"]["apply_calls"] == 1
    assert report["needs_recovery"]["notification_state"] == "not_applicable"
    request_ids = {
        report["applied"]["request_id"],
        report["changed_conflict"]["request_id"],
        report["dependency_conflict"]["request_id"],
        report["response_loss"]["request_id"],
        report["needs_recovery"]["request_id"],
    }
    assert len(request_ids) == 5
