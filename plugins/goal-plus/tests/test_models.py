from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from goal_plus.models import (
    AgentSessionHandle,
    AgentSessionRecord,
    ArtifactRef,
    Budget,
    CandidateProposal,
    CandidateRecord,
    CandidateTask,
    DevicePoolSpec,
    EditSurface,
    FrozenSpec,
    GoalPlusSpecDraft,
    IterationRecord,
    ModelSpec,
    PendingIntegrationRecord,
    ProviderResourceCleanupRecord,
    PublicationOutcome,
    PublicationRecoveryRecord,
    ResolvedEvidenceAnnotatorProfile,
    RunRecord,
    RunState,
    ScoreReport,
    SearchPlan,
    SearchSpec,
    SearchSpecDraft,
    SharedDirSpec,
    StrategySpec,
    ToolAdoptionRecord,
    ToolizationDecision,
    VerifierCommand,
    WorkerBudget,
    WorkerExecutionResult,
)
from goal_plus.runtime import FileSearchRuntime
from goal_plus.domain.search import BootstrapVerification, BootstrapVerifierResult, VERIFIER_ERROR_EXCERPT_CHARS
from goal_plus.domain.selection import SelectionSpec
from goal_plus.infrastructure.search_configuration import (
    SUPPLEMENTAL_EVALUATION_ENABLED_ENV,
    SUPPLEMENTAL_EVALUATION_REQUIRED_ENV,
)
from tests._runtime_helpers import make_project, spec_for


@pytest.mark.parametrize("metric_name", ["returncode", "elapsed_seconds"])
def test_search_metric_rejects_runtime_metadata(tmp_path, metric_name):
    from goal_plus.domain.search_input import SearchSpecInput

    data = spec_for(make_project(tmp_path)).model_dump(mode="json")
    data["metric_name"] = metric_name
    for model in (SearchSpec, SearchSpecInput):
        with pytest.raises(ValidationError, match="reserved runtime metadata"):
            model.model_validate(data)


def test_legacy_tool_adoption_declaration_drops_mode() -> None:
    adoption = ToolAdoptionRecord.model_validate(
        {
            "tool_id": "tool_001",
            "snapshot_hash": "abc123",
            "mode": "adapted",
        }
    )

    assert adoption.model_dump(mode="json") == {
        "tool_id": "tool_001",
        "snapshot_hash": "abc123",
    }


def test_candidate_task_drops_legacy_shared_dir_path() -> None:
    task = CandidateTask.model_validate(
        {
            "run_id": "run_1",
            "candidate_id": "c001",
            "hypothesis": "try a change",
            "workspace": ".",
            "shared_dir": ".gp/runs/run_1/shared",
            "allowed_files": ["initial_program.py"],
            "denied_files": ["evaluator.py"],
            "acceptance": {
                "objective": "legacy duplicate",
                "constraints": {"hidden": "must not be projected"},
            },
        }
    )

    assert "shared_dir" not in task.model_dump(mode="json")
    assert task.acceptance == []
    assert task.process_environment == {}


@pytest.mark.parametrize(
    ("pools", "resource_lock", "message"),
    [
        (
            [{"pool_id": "device"}, {"pool_id": "device"}],
            "device:0",
            "device pool ids must be unique",
        ),
        (
            [{"pool_id": "device"}],
            None,
            "is not referenced by a verifier resource_lock",
        ),
        (
            [{"pool_id": "device"}],
            "device:1",
            "only supports single-device resource_lock device:0",
        ),
    ],
)
def test_device_pools_require_unambiguous_logical_locks(
    pools: list[dict[str, str]],
    resource_lock: str | None,
    message: str,
) -> None:
    payload = {
        "objective": "test",
        "metric_name": "score",
        "metric_direction": "maximize",
        "source_path": ".",
        "edit_surface": {"allow": ["candidate.py"]},
        "budget": {"max_parallel": 1},
        "process_verifiers": [
            {
                "name": "score",
                "role": "ranking_signal",
                "command": ["true"],
                **({"resource_lock": resource_lock} if resource_lock else {}),
            }
        ],
        "device_pools": pools,
    }

    with pytest.raises(ValueError, match=message):
        SearchSpec.model_validate(payload)


def test_device_pool_ref_cannot_select_a_host_environment_variable() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DevicePoolSpec.model_validate(
            {"pool_id": "device", "visibility_env": "HOST_SECRET"}
        )


@pytest.mark.parametrize(
    "candidate_acceptance",
    [["criterion"] * 33, ["x" * 1001]],
)
def test_candidate_acceptance_is_bounded(candidate_acceptance: list[str]) -> None:
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(
            {
                "objective": "test",
                "metric_name": "score",
                "metric_direction": "maximize",
                "source_path": ".",
                "edit_surface": {"allow": ["candidate.py"]},
                "budget": {"max_parallel": 1},
                "process_verifiers": [
                    {
                        "name": "score",
                        "role": "ranking_signal",
                        "command": ["true"],
                    }
                ],
                "candidate_acceptance": candidate_acceptance,
            }
        )


def test_legacy_iteration_infers_shared_tool_publish_status() -> None:
    asset = {
        "asset_id": "legacy-asset",
        "candidate_id": "c001",
        "iteration": 1,
        "snapshot_hash": "abc123",
        "name": "legacy helper",
        "source_relative_path": "legacy-helper",
        "read_only_path": "/tmp/legacy-helper",
        "files": ["helper.py"],
        "size_bytes": 12,
        "created_at": "2026-08-03T00:00:00Z",
    }

    published = IterationRecord.model_validate(
        {
            "iteration": 1,
            "shared_tools": [asset],
            "created_at": "2026-08-03T00:00:00Z",
        }
    )
    partial = IterationRecord.model_validate(
        {
            "iteration": 1,
            "shared_tools": [asset],
            "shared_tool_errors": ["second asset rejected"],
            "created_at": "2026-08-03T00:00:00Z",
        }
    )
    unknown = IterationRecord.model_validate(
        {"iteration": 1, "created_at": "2026-08-03T00:00:00Z"}
    )

    assert published.shared_tool_publish_status == "published"
    assert partial.shared_tool_publish_status == "partially_published"
    assert unknown.shared_tool_publish_status == "legacy_unknown"
    assert not {
        "adopted_tools",
        "adoption_confounded",
        "toolization_decision",
        "toolization_advisories",
    } & set(
        unknown.model_dump(mode="json")
    )


def test_toolization_decision_enforces_positive_signals_and_exclusions() -> None:
    staged = ToolizationDecision.model_validate(
        {
            "outcome": "staged",
            "signals": ["repeated_workflow", "parser_trace_or_comparator"],
            "rationale": "  Encodes a repeated trace workflow.  ",
            "tool_names": ["trace-checker"],
        }
    )
    not_applicable = ToolizationDecision.model_validate(
        {
            "outcome": "not_applicable",
            "signals": [],
            "exclusion": "single_common_command",
            "rationale": "Only one ordinary command was used.",
            "tool_names": [],
        }
    )

    assert staged.rationale == "Encodes a repeated trace workflow."
    assert not_applicable.exclusion == "single_common_command"
    assert ToolizationDecision.model_validate(
        {
            "outcome": "not_applicable",
            "signals": [],
            "exclusion": "no_toolizable_material",
            "rationale": "The cumulative review found no reusable tool material.",
            "tool_names": [],
        }
    ).exclusion == "no_toolizable_material"
    current_signals = [
        "repeated_workflow",
        "domain_construction_or_probe",
        "behavior_or_invariant_checker",
        "reproducer_fixture_or_case_generator",
        "parser_trace_or_comparator",
        "peer_setup_or_feedback_reduction",
    ]
    for signal in current_signals:
        decision = ToolizationDecision.model_validate(
            {
                "outcome": "staged",
                "signals": [signal],
                "rationale": f"Exercises {signal}.",
                "tool_names": ["diagnostic-helper"],
            }
        )
        assert decision.signals == [signal]

    for removed_signal in [
        "repeated_sequence",
        "domain_probe",
        "parser_or_trace",
        "peer_setup_reduction",
    ]:
        with pytest.raises(ValidationError):
            ToolizationDecision.model_validate(
                {
                    "outcome": "staged",
                    "signals": [removed_signal],
                    "rationale": "Removed signal must not enter a new run.",
                    "tool_names": ["invalid-helper"],
                }
            )
    assert ToolizationDecision.model_validate(
        {
            "outcome": "not_applicable",
            "signals": [],
            "exclusion": "existing_family_sufficient",
            "rationale": "The current family head already covers this workflow.",
            "tool_names": [],
        }
    ).exclusion == "existing_family_sufficient"
    assert ToolizationDecision.model_validate(
        {
            "outcome": "not_applicable",
            "signals": [],
            "exclusion": "max_published_versions_reached",
            "rationale": "The family has reached its configured version limit.",
            "tool_names": [],
        }
    ).exclusion == "max_published_versions_reached"

    for payload, message in [
        (
            {
                "outcome": "staged",
                "signals": [],
                "rationale": "No positive signal.",
                "tool_names": ["helper"],
            },
            "requires at least one signal",
        ),
        (
            {
                "outcome": "staged",
                "signals": ["behavior_or_invariant_checker"],
                "rationale": "No named staged tool.",
                "tool_names": [],
            },
            "requires at least one tool name",
        ),
        (
            {
                "outcome": "not_applicable",
                "signals": [],
                "rationale": "No concrete exclusion.",
                "tool_names": [],
            },
            "requires an exclusion",
        ),
    ]:
        with pytest.raises(ValidationError, match=message):
            ToolizationDecision.model_validate(payload)


def test_shared_dir_family_governance_defaults() -> None:
    spec = SharedDirSpec(enabled=True)
    assert spec.max_pending_revisions_per_family == 1
    assert spec.max_published_versions_per_family == 2


def valid_spec_dict() -> dict:
    return {
        "objective": "maximize toy score",
        "metric_name": "combined_score",
        "metric_direction": "maximize",
        "source_path": ".",
        "edit_surface": {
            "allow": ["initial_program.py"],
            "deny": ["evaluator.py"]},
        "budget": {
            "max_parallel": 2},
        "process_verifiers": [
            {
                "name": "score",
                "role": "ranking_signal",
                "command": ["python", "evaluator.py"]}
        ]}


def test_search_spec_parses_nested_models_and_serializes_enums() -> None:
    spec = SearchSpec.model_validate(valid_spec_dict())

    assert isinstance(spec.budget, Budget)
    assert isinstance(spec.edit_surface, EditSurface)
    assert isinstance(spec.process_verifiers[0], VerifierCommand)
    assert isinstance(spec.strategy, StrategySpec)

    dumped = spec.model_dump(mode="json")
    assert dumped["process_verifiers"][0]["role"] == "ranking_signal"
    assert dumped["metric_direction"] == "maximize"
    assert dumped["strategy"]["name"] == "agent_guided"
    assert dumped["strategy"]["orchestration_mode"] == "parallel_loops"
    assert "worker_host" not in dumped["strategy"]
    assert "models" not in dumped["strategy"]


def test_required_supplemental_evaluation_rejects_disabled_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    data = valid_spec_dict()
    data["source_path"] = str(project)
    monkeypatch.setenv(SUPPLEMENTAL_EVALUATION_ENABLED_ENV, "0")
    monkeypatch.setenv(SUPPLEMENTAL_EVALUATION_REQUIRED_ENV, "1")

    with pytest.raises(
        ValueError,
        match="requires GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED=1",
    ):
        FileSearchRuntime(tmp_path / ".gp-missing").freeze_spec(
            SearchSpec.model_validate(data), [project / "evaluator.py"], agent_harness="codex"
        )


def test_supplemental_evaluation_does_not_change_frozen_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    data = valid_spec_dict()
    data["source_path"] = str(project)
    monkeypatch.setenv(SUPPLEMENTAL_EVALUATION_ENABLED_ENV, "1")
    monkeypatch.setenv(SUPPLEMENTAL_EVALUATION_REQUIRED_ENV, "1")

    frozen = FileSearchRuntime(tmp_path / ".gp").freeze_spec(
        SearchSpec.model_validate(data), [project / "evaluator.py"], agent_harness="codex"
    )

    frozen_spec = frozen.spec.model_dump(mode="json")
    assert "acceptance_view" not in frozen_spec
    assert "supplemental_evaluation" not in frozen_spec


def test_goal_plus_spec_draft_exposes_typed_partial_search_spec() -> None:
    draft = GoalPlusSpecDraft(
        baseline={},
        metric={"name": "combined_score"},
        correctness_gate={},
        edit_surface={},
        search_spec={
            "metric_name": "combined_score",
            "edit_surface": {"allow": ["solution.cpp"]},
            "process_verifiers": [
                {
                    "name": "public_score",
                    "role": "ranking_signal",
                    "command": ["python", "verify.py"],
                }
            ],
        },
        promotion_rule="highest public score",
        confidence="medium",
        open_questions=["Confirm the source path."],
    )

    assert isinstance(draft.search_spec, SearchSpecDraft)
    assert draft.search_spec.edit_surface is not None
    assert draft.search_spec.edit_surface.allow == ["solution.cpp"]
    assert draft.model_dump(mode="json")["search_spec"] == {
        "metric_name": "combined_score",
        "edit_surface": {
            "allow": ["solution.cpp"],
            "deny": [],
        },
        "process_verifiers": [
            {
                "name": "public_score",
                "role": "ranking_signal",
                "command": ["python", "verify.py"],
                "cwd": ".",
                "timeout_seconds": 300,
                "feedback_policy": "visible_to_workers",
            }
        ],
    }


def test_goal_plus_spec_draft_keeps_legacy_unstructured_search_spec_readable() -> None:
    draft = GoalPlusSpecDraft(
        baseline={},
        metric={},
        correctness_gate={},
        edit_surface={},
        search_spec={"allowed_paths": ["legacy.py"], "process_verifier": "old"},
        promotion_rule="legacy record",
        confidence="high",
    )

    assert isinstance(draft.search_spec, dict)
    assert draft.model_dump(mode="json")["search_spec"]["allowed_paths"] == [
        "legacy.py"
    ]


def test_model_spec_requires_a_concrete_model_reference() -> None:
    with pytest.raises(ValidationError):
        ModelSpec(model="")

    model = ModelSpec(model="gpt", count=1)
    assert model.model_dump(mode="json") == {
        "model": "gpt",
        "count": 1,
        "provider": None,
        "adapter_version": None,
        "reasoning_effort": None,
        "service_tier": None,
        "context_policy": {},
    }


def test_verifier_resource_lock_rejects_blank_names() -> None:
    command = {
        "name": "score",
        "role": "ranking_signal",
        "command": ["python", "verify.py"],
        "resource_lock": "device:0",
    }
    assert VerifierCommand.model_validate(command).resource_lock == "device:0"
    command["resource_lock"] = "  device:0  "
    assert VerifierCommand.model_validate(command).resource_lock == "device:0"

    command["resource_lock"] = " "
    with pytest.raises(ValidationError, match="resource_lock must be non-empty"):
        VerifierCommand.model_validate(command)


def test_search_spec_supports_registered_workspace_providers() -> None:
    default_spec = SearchSpec.model_validate(valid_spec_dict())
    assert default_spec.workspace.provider == "git_worktree"
    assert default_spec.model_dump(mode="json")["workspace"] == {
        "provider": "git_worktree",
        "promotion_mode": "apply",
    }

    copy_data = valid_spec_dict()
    copy_data["workspace"] = {"provider": "copy"}
    copy_spec = SearchSpec.model_validate(copy_data)
    assert copy_spec.workspace.provider == "copy"
    assert copy_spec.workspace.promotion_mode == "artifact_only"

    thinkthread_data = valid_spec_dict()
    thinkthread_data["workspace"] = {"provider": "thinkthread"}
    thinkthread_spec = SearchSpec.model_validate(thinkthread_data)
    assert thinkthread_spec.workspace.provider == "thinkthread"
    assert thinkthread_spec.workspace.promotion_mode == "apply"

    thinkthread_data["workspace"]["dependencies"] = [
        {"path": "pkg", "scope": "tree_content"},
        {"path": "config.json", "scope": "content"},
    ]
    thinkthread_spec = SearchSpec.model_validate(thinkthread_data)
    assert [item.path for item in thinkthread_spec.workspace.dependencies] == [
        "config.json",
        "pkg",
    ]

    non_thinkthread_dependencies = valid_spec_dict()
    non_thinkthread_dependencies["workspace"] = {
        "provider": "git_worktree",
        "dependencies": [{"path": "config.json", "scope": "content"}],
    }
    with pytest.raises(ValidationError, match="require provider='thinkthread'"):
        SearchSpec.model_validate(non_thinkthread_dependencies)

    thinkthread_data["workspace"]["dependencies"] = [
        {"path": "../escape", "scope": "content"}
    ]
    with pytest.raises(ValidationError, match="snapshot-relative"):
        SearchSpec.model_validate(thinkthread_data)

    invalid_copy_data = valid_spec_dict()
    invalid_copy_data["workspace"] = {
        "provider": "copy",
        "promotion_mode": "apply",
    }
    with pytest.raises(ValidationError, match="artifact_only"):
        SearchSpec.model_validate(invalid_copy_data)

    invalid_data = valid_spec_dict()
    invalid_data["workspace"] = {"provider": "overlay"}
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(invalid_data)


def test_artifact_ref_enforces_provider_kind_pairing() -> None:
    git_ref = ArtifactRef(
        kind="git_commit",
        id="abc123",
        provider="git_worktree",
    )
    snapshot_ref = ArtifactRef(
        kind="fs_snapshot",
        id="snap_123",
        provider="thinkthread",
    )

    assert git_ref.model_dump(mode="json")["provider"] == "git_worktree"
    assert snapshot_ref.kind == "fs_snapshot"
    with pytest.raises(ValidationError, match="incompatible"):
        ArtifactRef(
            kind="git_commit",
            id="snap_123",
            provider="thinkthread",
        )


def test_search_spec_requires_structured_strategy() -> None:
    data = valid_spec_dict()
    data["strategy"] = {
        "name": "agent_guided",
        "inner_agent": "common",
    }
    spec = SearchSpec.model_validate(data)
    assert spec.strategy.name == "agent_guided"
    assert "worker_host" not in type(spec.strategy).model_fields
    assert spec.strategy.inner_agent == "common"
    assert "worker_agent_type" not in spec.strategy.model_dump(mode="json")

    legacy = valid_spec_dict()
    legacy["strategy"] = {
        "name": "agent_guided",
        "worker_agent_type": "search_candidate_agent",
    }
    with pytest.raises(ValidationError, match="worker_agent_type"):
        SearchSpec.model_validate(legacy)

    legacy_string = valid_spec_dict()
    legacy_string["strategy"] = "evolve"
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(legacy_string)

    for retired_field, retired_value in (
        ("worker_mode", "agent-session-pool"),
        ("history_policy", {"scope": "top_n"}),
        ("driver", "builtin"),
        ("parent_policy", "best"),
    ):
        data = valid_spec_dict()
        data["strategy"] = {"name": "agent_guided", retired_field: retired_value}
        with pytest.raises(ValidationError):
            SearchSpec.model_validate(data)


@pytest.mark.parametrize("agent_harness", ["codex", "pi", "thinkthread"])
def test_strategy_spec_cannot_select_worker_host(agent_harness) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StrategySpec.model_validate({"worker_host": agent_harness})


@pytest.mark.parametrize("agent", ["pi", "codex"])
def test_strategy_spec_cannot_select_agent_profile(agent) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StrategySpec.model_validate({"worker_launch": {"agent_profile": agent}})


def test_thinkthread_execution_and_publication_models_round_trip() -> None:
    snapshot = ArtifactRef(
        kind="fs_snapshot",
        id="fsnap-selected",
        provider="thinkthread",
    )
    execution = WorkerExecutionResult(
        output="done",
        execution_scope="thinkthread_private",
        artifact_ref=snapshot,
        thinkthread_id="tt-child-1",
        evidence_refs=["evidence-1"],
    )
    assert WorkerExecutionResult.model_validate(
        execution.model_dump(mode="json")
    ) == execution

    recovery = PublicationRecoveryRecord(
        request_id="req-apply-1",
        attempts=1,
        updated_at="2026-09-03T00:00:00Z",
    )
    outcome = PublicationOutcome(
        run_id="run-1",
        revision=1,
        state="recovering",
        base_artifact_ref=snapshot.model_copy(update={"id": "fsnap-base"}),
        selected_artifact_ref=snapshot,
        publication_artifact_ref=snapshot.model_copy(update={"id": "fsnap-publish"}),
        apply_request_id="req-apply-1",
        apply_outcome="needs_recovery",
        recovery=recovery,
        updated_at="2026-09-03T00:00:00Z",
    )
    assert PublicationOutcome.model_validate(
        outcome.model_dump(mode="json")
    ) == outcome

    pending = PendingIntegrationRecord(
        run_id="run-1",
        base_artifact_ref=snapshot.model_copy(update={"id": "fsnap-base"}),
        selected_artifact_ref=snapshot,
        publication_artifact_ref=snapshot.model_copy(update={"id": "fsnap-publish"}),
        apply_request_id="req-apply-1",
        apply_outcome="conflict",
        changed_paths=["src/a.py"],
        evidence_refs=["evidence-1"],
        created_at="2026-09-03T00:00:00Z",
    )
    assert PendingIntegrationRecord.model_validate(
        pending.model_dump(mode="json")
    ) == pending


def test_thinkthread_execution_and_publication_models_fail_closed() -> None:
    with pytest.raises(ValidationError, match="requires a ThinkThread identity"):
        WorkerExecutionResult(
            execution_scope="thinkthread_private",
            artifact_ref={
                "kind": "fs_snapshot",
                "id": "fsnap-selected",
                "provider": "thinkthread",
            },
        )
    with pytest.raises(ValidationError, match="cannot carry a ThinkThread identity"):
        WorkerExecutionResult(
            execution_scope="native_root",
            thinkthread_id="tt-child-1",
        )
    with pytest.raises(ValidationError, match="cannot queue a notification"):
        PublicationOutcome(
            run_id="run-1",
            revision=1,
            state="recovering",
            base_artifact_ref={
                "kind": "fs_snapshot",
                "id": "fsnap-base",
                "provider": "thinkthread",
            },
            selected_artifact_ref={
                "kind": "fs_snapshot",
                "id": "fsnap-selected",
                "provider": "thinkthread",
            },
            publication_artifact_ref={
                "kind": "fs_snapshot",
                "id": "fsnap-publish",
                "provider": "thinkthread",
            },
            apply_outcome="needs_recovery",
            recovery={
                "request_id": "req-apply-1",
                "updated_at": "2026-09-03T00:00:00Z",
            },
            notification_state="pending",
            updated_at="2026-09-03T00:00:00Z",
        )


def test_legacy_run_keeps_unknown_search_and_publication_state() -> None:
    run = RunRecord.model_validate(
        {
            "run_id": "run-legacy",
            "state": RunState.RUNNING,
            "frozen_spec_id": "spec-legacy",
            "source_path": ".",
            "created_at": "2026-09-03T00:00:00Z",
        }
    )

    assert run.search_result == "unknown"
    assert run.publication_outcome is None


def test_provider_cleanup_requires_pins_or_a_durable_request() -> None:
    with pytest.raises(ValidationError, match="requires at least one pin"):
        ProviderResourceCleanupRecord(
            resource_id="fsnap-owned",
            state="retained",
            created_at="2026-09-03T00:00:00Z",
            updated_at="2026-09-03T00:00:00Z",
        )
    with pytest.raises(ValidationError, match="requires a RequestId"):
        ProviderResourceCleanupRecord(
            resource_id="fsnap-owned",
            state="removing",
            created_at="2026-09-03T00:00:00Z",
            updated_at="2026-09-03T00:00:00Z",
        )


def test_selection_keys_are_enumerated_and_unique():
    schema = SelectionSpec.model_json_schema()
    assert schema["properties"]["ranking_keys"]["items"]["enum"] == ["hard_score", "quality_score"]
    with pytest.raises(ValidationError, match="hard_score.*quality_score"):
        SelectionSpec(ranking_keys=["score"])
    with pytest.raises(ValidationError, match="unique"):
        SelectionSpec(ranking_keys=["hard_score", "hard_score"], ranking_tolerance=[0, 0])


@pytest.mark.parametrize("suite", ["process_verifiers", "promotion_verifiers"])
def test_search_input_rejects_multiple_ranking_sources(suite):
    from goal_plus.domain.search_input import SearchSpecInput

    command = {"name": "score", "role": "ranking_signal", "command": ["true"]}
    data = {
        "objective": "test", "metric_name": "score", "metric_direction": "maximize",
        "source_path": ".", "edit_surface": {"allow": ["candidate.py"]},
        "budget": {"max_parallel": 1}, "process_verifiers": [command],
    }
    data[suite] = [command, {**command, "name": "other"}]
    for model in (SearchSpec, SearchSpecInput):
        with pytest.raises(ValidationError, match="at most one ranking_signal"):
            model.model_validate(data)


def test_bootstrap_failure_detail_selects_blocking_failure_and_bounds_error():
    diagnostic = BootstrapVerifierResult(
        name="diagnostic", role="diagnostic_signal", passed=False, failure_class="VerifierCommandFailed",
    )
    failure = BootstrapVerifierResult(
        name="ranking", role="ranking_signal", passed=False, failure_class="VerifierCommandFailed",
        metrics={"returncode": 0, "error": "x" * (VERIFIER_ERROR_EXCERPT_CHARS + 100)},
        log_path=Path("/logs/ranking.log"),
    )
    report = BootstrapVerification(
        phase="origin", workspace_provider="thinkthread", passed=False,
        verifier_results=[diagnostic, BootstrapVerifierResult(
            name="candidate", role="validity_gate", passed=False, failure_class="CandidateRejected",
        ), failure], started_at="start", completed_at="end",
    )
    assert not report.evaluation_completed
    detail = report.failure_detail()
    assert "ranking: VerifierCommandFailed" in detail and "diagnostic" not in detail
    assert "returncode=0" in detail and "log_path=/logs/ranking.log" in detail
    assert "[truncated]" in detail
    assert "x" * (VERIFIER_ERROR_EXCERPT_CHARS + 1) not in detail


def test_bootstrap_cannot_ignore_anti_cheat_rejection():
    report = BootstrapVerification(
        phase="origin", workspace_provider="copy", passed=False,
        verifier_results=[BootstrapVerifierResult(
            name="integrity", role="anti_cheat_gate", passed=False, failure_class="CandidateRejected",
        )], started_at="start", completed_at="end",
    )
    assert not report.evaluation_completed
    assert "integrity: CandidateRejected" in report.failure_detail()


def test_strategy_spec_accepts_supported_orchestration_modes() -> None:
    default = StrategySpec()
    rolling = StrategySpec(orchestration_mode="rolling_candidates")
    parallel = StrategySpec(orchestration_mode="parallel_loops")

    assert default.orchestration_mode == "parallel_loops"
    assert rolling.orchestration_mode == "rolling_candidates"
    assert parallel.orchestration_mode == "parallel_loops"

    with pytest.raises(ValidationError):
        StrategySpec(orchestration_mode="conductor")  # type: ignore[arg-type]


def test_strategy_spec_derives_llm_verifier_requirement() -> None:
    hard_only = StrategySpec()
    minimal = StrategySpec(
        orchestration_mode="parallel_loops",
        selection={
            "ranking_keys": ["quality_score", "hard_score"],
            "ranking_tolerance": [0.05, 0.0],
        },
    )
    adaptive = StrategySpec(
        orchestration_mode="adaptive_search",
        allocation={"policy": "priority_replace/v1"},
    )

    assert hard_only.requires_llm_verifier is False
    assert minimal.requires_llm_verifier is True
    assert adaptive.requires_llm_verifier is True

    with pytest.raises(ValidationError, match="requires allocation"):
        StrategySpec(orchestration_mode="adaptive_search")
    for non_adaptive_mode in ("parallel_loops", "rolling_candidates"):
        with pytest.raises(ValidationError, match="requires.*adaptive_search"):
            StrategySpec(
                orchestration_mode=non_adaptive_mode,
                allocation=adaptive.allocation,
            )
    with pytest.raises(
        ValidationError,
        match="quality_score selection requires parallel_loops or adaptive_search",
    ):
        StrategySpec(
            orchestration_mode="rolling_candidates",
            selection={
                "ranking_keys": ["quality_score"],
                "ranking_tolerance": [0.0],
            },
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StrategySpec.model_validate({"verifier": {}})


def test_adaptive_verifier_rules_are_owned_by_frozen_policy(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    spec_data = spec_for(project).model_dump(mode="json")
    spec_data["workspace"] = {"provider": "git_worktree"}
    spec_data["budget"]["max_candidates"] = (
        spec_data["budget"]["max_parallel"] + 1
    )
    spec_data["strategy"]["orchestration_mode"] = "adaptive_search"
    spec_data["strategy"]["worker_budget"] = {"max_runtime_seconds": 60}
    spec_data["strategy"]["selection"] = {
        "ranking_keys": ["quality_score", "hard_score"],
        "ranking_tolerance": [0.05, 0.0],
    }
    spec_data["strategy"]["allocation"] = {"policy": "priority_replace/v1"}
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"], agent_harness="codex",
    )
    assert frozen.spec.strategy.allocation is not None
    rules = frozen.spec.strategy.allocation.verifier_rules()
    assert rules.policy_id == "priority_replace/v1"
    assert rules.score_names == [
        "progress_reward",
        "allocation_utility",
    ]


def test_strategy_spec_accepts_worker_budget() -> None:
    spec = StrategySpec(
        worker_budget={
            "max_runtime_seconds": 600,
            "max_turns": 8,
            "on_exceed": "interrupt",
        },
    )

    assert isinstance(spec.worker_budget, WorkerBudget)
    assert spec.worker_budget.max_runtime_seconds == 600
    assert spec.worker_budget.max_turns == 8
    assert spec.worker_budget.on_exceed == "interrupt"
    assert spec.model_dump(mode="json")["worker_budget"] == {
        "max_runtime_seconds": 600,
        "max_turns": 8,
        "on_exceed": "interrupt",
    }


@pytest.mark.codex
def test_strategy_spec_accepts_codex_worker_launch_options() -> None:
    spec = StrategySpec(
        worker_launch={
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
            "service_tier": "priority",
        },
    )

    assert spec.model_dump(mode="json")["worker_launch"] == {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "service_tier": "priority",
    }


def test_evidence_annotator_config_is_optional_and_overridable() -> None:
    inherited = StrategySpec()
    extended_timeout = StrategySpec(
        evidence_annotator={"timeout_seconds": 1800}
    )
    explicit = StrategySpec(
        evidence_annotator={
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "timeout_seconds": 90,
        }
    )
    pi_explicit = StrategySpec(
        evidence_annotator={
            "model": "deepseek/deepseek-chat",
        },
    )

    assert inherited.model_dump(mode="json")["evidence_annotator"] == {
        "model": None,
        "reasoning_effort": None,
        "timeout_seconds": 1800,
    }
    assert extended_timeout.evidence_annotator.timeout_seconds == 1800
    with pytest.raises(ValidationError):
        StrategySpec(evidence_annotator={"timeout_seconds": 1801})
    assert explicit.model_dump(mode="json")["evidence_annotator"] == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "timeout_seconds": 90,
    }
    assert pi_explicit.model_dump(mode="json")["evidence_annotator"] == {
        "model": "deepseek/deepseek-chat",
        "reasoning_effort": None,
        "timeout_seconds": 1800,
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StrategySpec(evidence_annotator={"unsupported": "value"})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StrategySpec(
            evidence_annotator={"agent_harness": "codex"},
        )


def test_internal_agent_profiles_have_only_host_owned_launch_fields() -> None:
    assert set(ResolvedEvidenceAnnotatorProfile.model_fields) == {
        "agent_harness",
        "runtime_provider",
        "model",
        "reasoning_effort",
        "timeout_seconds",
        "configuration_source",
    }


def test_worker_budget_requires_runtime_or_turn_limit() -> None:
    with pytest.raises(ValidationError):
        WorkerBudget()

    with pytest.raises(ValidationError):
        WorkerBudget(max_runtime_seconds=0)

    with pytest.raises(ValidationError):
        WorkerBudget(max_turns=0)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorkerBudget(min_runtime_seconds=300, max_turns=8)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorkerBudget(min_runtime_seconds=600, max_runtime_seconds=600)


def test_strategy_plan_models_capture_initial_independent_proposals() -> None:
    plan = SearchPlan(
        run_id="run_1",
        plan_id="plan_001",
        strategy=StrategySpec(name="agent_guided"),
        requested_k=4,
        planned_k=2,
        remaining_budget=2,
        requires_agent_proposals=True,
        created_at="2026-06-24T00:00:00Z",
    )
    proposal = CandidateProposal(
        intent="try an independent implementation",
        expected_tradeoff="higher score with more risk",
    )

    assert plan.requires_agent_proposals is True
    assert proposal.intent == "try an independent implementation"

    with pytest.raises(ValidationError):
        CandidateProposal(
            intent="mutate c001",
            parent_candidate_ids=["c001"],  # type: ignore[call-arg]
        )


def test_agent_session_record_is_context_handle_with_required_candidate() -> None:
    session = AgentSessionRecord(
        agent_session_id="agent_001",
        run_id="run_1",
        candidate_id="c001",
        created_at="2026-06-24T00:00:00Z",
        updated_at="2026-06-24T00:00:00Z",
        workspace=Path("/tmp/c001"),
        directive={"goal": "try one direction"},
        launch={
            "transport": "app-server",
            "description": "c001 try one direction",
            "prompt": "agent_session_id=agent_001; candidate_id=c001; idea: try one direction",
        },
        counters={"verifier_runs": 0},
    )
    assert session.candidate_id == "c001"
    assert session.agent_harness == "codex"
    assert session.session_handle == AgentSessionHandle(agent_harness="codex")
    assert session.launch["transport"] == "app-server"

    # candidate_id is now required - a subagent session without a candidate
    # has no useful role in this runtime.
    with pytest.raises(ValidationError):
        AgentSessionRecord(  # type: ignore[call-arg]
            agent_session_id="agent_002",
            run_id="run_1",
            created_at="2026-06-24T00:00:00Z",
            updated_at="2026-06-24T00:00:00Z",
            workspace=Path("/tmp/c001"),
        )


def test_search_spec_rejects_invalid_budget_and_blank_source_path() -> None:
    assert Budget(max_parallel=2, max_candidates=None).max_candidates is None
    assert Budget(max_parallel=2, max_candidates=2).max_candidates == 2
    with pytest.raises(ValidationError, match="must be >= budget.max_parallel"):
        Budget(max_parallel=2, max_candidates=1)

    data = valid_spec_dict()
    data["budget"]["max_parallel"] = 0
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(data)

    data = valid_spec_dict()
    data["source_path"] = "   "
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(data)


def test_models_reject_extra_fields() -> None:
    data = valid_spec_dict()
    data["unexpected"] = True

    with pytest.raises(ValidationError):
        SearchSpec.model_validate(data)


def test_shared_dir_is_opt_in_and_bounded() -> None:
    default_spec = SearchSpec.model_validate(valid_spec_dict())
    assert default_spec.shared_dir.enabled is False

    data = valid_spec_dict()
    data["shared_dir"] = {
        "enabled": True,
        "max_tools_per_iteration": 4,
        "max_files_per_iteration": 12,
        "max_path_entries_per_iteration": 96,
        "max_depth": 5,
        "max_bytes_per_iteration": 4096,
    }
    spec = SearchSpec.model_validate(data)
    assert spec.shared_dir.enabled is True
    assert spec.shared_dir.max_tools_per_iteration == 4
    assert spec.shared_dir.max_files_per_iteration == 12
    assert spec.shared_dir.max_path_entries_per_iteration == 96
    assert spec.shared_dir.max_depth == 5
    assert spec.shared_dir.max_bytes_per_iteration == 4096

    data["shared_dir"]["max_files_per_iteration"] = 0
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(data)

    data["shared_dir"]["max_files_per_iteration"] = 12
    data["shared_dir"]["max_depth"] = 0
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(data)


def test_candidate_record_rejects_submitted_status() -> None:
    task = CandidateTask(
        run_id="run_1",
        candidate_id="c001",
        hypothesis="try one",
        workspace=Path("/tmp/c001"),
        allowed_files=["initial_program.py"],
        denied_files=["evaluator.py"],
    )

    with pytest.raises(ValidationError):
        CandidateRecord(
            candidate_id="c001",
            status="submitted",  # type: ignore[arg-type]
            task=task,
        )


def test_candidate_record_accepts_created_and_evaluated() -> None:
    task = CandidateTask(
        run_id="run_1",
        candidate_id="c001",
        hypothesis="try one",
        workspace=Path("/tmp/c001"),
        allowed_files=["initial_program.py"],
        denied_files=["evaluator.py"],
    )

    for status in ("created", "evaluated", "failed"):
        CandidateRecord(
            candidate_id="c001",
            status=status,
            task=task,
        )
