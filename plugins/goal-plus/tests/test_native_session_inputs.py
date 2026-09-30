"""Native hosts preserve diagnostic policy without inheriting parent identities."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from goal_plus.codex_hook_input import CODEX_CANDIDATE_SESSION_ENV, CODEX_ORDINARY_ROLE_ENV
from goal_plus.domain.goal import GoalPlusFinalCheckerIdentity
from goal_plus.domain.search import AgentSessionRecord
from goal_plus.evidence_annotator import _annotation_transcript
from goal_plus.infrastructure.final_check_binding import FINAL_CHECK_IDENTITY_ENV
from goal_plus.infrastructure.native_session_inputs import (
    native_candidate_input, native_final_check_input, native_work_item_input,
)
from goal_plus.internal_agents import inherited_host_environment


@pytest.mark.parametrize("harness", ["codex", "pi"])
@pytest.mark.parametrize("role", ["candidate", "candidate-resume", "final-checker", "ordinary", "ordinary-resume"])
@pytest.mark.parametrize("debug", [None, "1", "0"])
def test_native_diagnostic_policy_reaches_internal_annotator(
    tmp_path: Path, monkeypatch, harness: str, role: str,
    debug: str | None,
):
    if debug is None:
        monkeypatch.delenv("GOAL_PLUS_DEBUG", raising=False)
    else:
        monkeypatch.setenv("GOAL_PLUS_DEBUG", debug)
    parent_identity_keys = (
        "CODEX_THREAD_ID", "GOAL_PLUS_ROOT", "GOAL_PLUS_SEARCH_ROOT",
        "GOAL_PLUS_PI_ROLE", "GOAL_PLUS_AGENT_SESSION_ID",
        "GOAL_PLUS_INTERNAL_AGENT_HARNESS", CODEX_CANDIDATE_SESSION_ENV,
        CODEX_ORDINARY_ROLE_ENV, FINAL_CHECK_IDENTITY_ENV,
    )
    for key in parent_identity_keys:
        monkeypatch.setenv(key, "parent-only")
    monkeypatch.setenv("GOAL_PLUS_PI_DEV_ROOT", "installation-root")
    monkeypatch.setenv("GOAL_PLUS_PYTHON", "installation-python")
    monkeypatch.setenv("NATIVE_INPUT_TEST_MARKER", "keep")
    scope = {"agent_harness": harness, "native_agent_config": (
        {"model": "main-model", "reasoning_effort": "low"} if harness == "codex"
        else {"model_pattern": "provider/main-model", "thinking_level": "low"}
    )}
    if role.startswith("candidate"):
        session = AgentSessionRecord(
            agent_session_id="worker_1", run_id="run_1", candidate_id="c001",
            created_at="2026-09-28T00:00:00Z", updated_at="2026-09-28T00:00:00Z",
            workspace=tmp_path, agent_harness=harness,
            session_handle={"agent_harness": harness, "external_id": "native-worker"},
        )
        native = native_candidate_input(
            tmp_path, session, scope, process_environment={}, resume=role.endswith("resume"),
        )
    elif role == "final-checker":
        native = native_final_check_input(tmp_path, tmp_path, GoalPlusFinalCheckerIdentity(
            goal_plus_id="goal_1", check_id="check_1", goal_revision=1,
        ), scope)
    else:
        native = native_work_item_input(
            tmp_path, tmp_path, "ordinary_1", scope, resume=role.endswith("resume"),
        )
    environment = native.process.environment
    assert environment.get("GOAL_PLUS_DEBUG") == debug
    assert environment["GOAL_PLUS_ROOT"] == str(tmp_path)
    assert environment["GOAL_PLUS_PI_DEV_ROOT"] == "installation-root"
    assert environment["GOAL_PLUS_PYTHON"] == "installation-python"
    assert environment["NATIVE_INPUT_TEST_MARKER"] == "keep"
    assert all(environment.get(key) != "parent-only" for key in parent_identity_keys)

    # Follow the worker -> internal-agent inheritance boundary to the real recorder factory.
    with patch.dict(os.environ, environment, clear=True):
        internal_environment = inherited_host_environment(harness)
    with patch.dict(os.environ, internal_environment, clear=True):
        capture = _annotation_transcript({
            "runtime_root": str(tmp_path), "run_id": "run_1", "candidate_id": "c001",
            "iteration": 3, "_annotation_attempt": 2, "_annotation_task_id": "task_1",
            "_annotation_settlement_id": "settlement_3", "exact_attempt_commit": "commit_3",
        })
    enabled = debug == "1"
    assert (capture is not None) == enabled
    if enabled:
        assert capture.identity.role == "annotator"
        assert capture.identity.candidate_id == "c001"
        assert capture.identity.iteration == 3
        assert capture.identity.attempt == 2
        assert capture.identity.settlement_id == "settlement_3"
        assert capture.identity.artifact["exact_attempt_commit"] == "commit_3"
