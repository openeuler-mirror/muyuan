
from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus.domain.evidence import CandidateAttemptFeedback, ScoreReport
from goal_plus.host_services import SearchHostService
from goal_plus.models import SearchSpec
from goal_plus.runtime import FileSearchRuntime
from goal_plus.infrastructure.host_bindings import candidate_inner_agent_binding
from goal_plus.application.candidate_host import ThinkThreadCandidatePreparation
from goal_plus.application.candidate_queries import CandidateHostQueries
from goal_plus.domain.adaptive.models import ExpansionSource
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import IterationRecord, ResultLedgerEntry
from goal_plus.domain.search import CandidateProposal
from goal_plus.domain.worker_policy import normalize_worker_budget_override
from goal_plus.ports.candidate_creation import CandidateMaterialization
from tests._runtime_helpers import make_project, spec_for
from tests._thinkthread_sdk import IntegratedAgentPosix, thinkthread_spec


@pytest.fixture
def candidate_host(tmp_path, monkeypatch, request):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path.parent / (tmp_path.name + "-session-runtime"))
    spec = thinkthread_spec(project)
    if getattr(request, "param", None) == "no_budget":
        spec.strategy.worker_budget = None
    elif getattr(request, "param", None) == "adaptive":
        payload = spec.model_dump(mode="json")
        payload["strategy"]["orchestration_mode"] = "adaptive_search"
        payload["strategy"]["allocation"] = {"policy": "swe_search_uct/v1"}
        spec = SearchSpec.model_validate(payload)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    session = runtime.start_agent_session(run_id, tasks[0].candidate_id)
    session = runtime.bind_agent_handle(
        session.agent_session_id,
        {"agent_harness": "pi", "runtime_provider": "thinkthread", "external_id": "tt-child-test"},
    )
    return runtime, SearchHostService(runtime.root_dir, runtime=runtime), tasks, session


def domain_files(root: Path):
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.json")
    }




def test_native_wake_rejects_pending_allocation_retirement(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["workspace"] = {"provider": "git_worktree"}
    spec_data["strategy"] = {
        "name": "random",
        "orchestration_mode": "adaptive_search",
        "selection": {
            "ranking_keys": ["quality_score", "hard_score"],
            "ranking_tolerance": [0.05, 0.0],
        },
        "verifier": {"scores": ["progress_reward", "allocation_utility"]},
        "allocation": {"policy": "swe_search_uct/v1"},
    }
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
        agent_harness="codex",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    task = runtime.start_batch(
        run_id, runtime.plan_next(run_id, requested_k=1).plan_id
    )[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    queries = SearchHostService(runtime.root_dir, runtime=runtime).queries
    queries.source.load_allocation_decisions = lambda _run_id: [
        SimpleNamespace(
            status="pending",
            retire_candidate_ids=[task.candidate_id],
            decision_id="allocation_test",
        )
    ]

    with pytest.raises(RuntimeError, match="active allocation decision"):
        queries.require_native_execution(session.agent_session_id, now=0.0)


@pytest.mark.parametrize("invalidated", [False, True])
def test_prepare_session_typed_payload(candidate_host, invalidated):
    runtime, service, tasks, session = candidate_host
    if invalidated:
        run = runtime.search_run.state.read_run(session.run_id)
        run.invalidated_at = session.created_at
        run.invalidation_reason = "verifier_target_mismatch"
        runtime.search_run.persist(run)
        before = domain_files(runtime.root_dir)
        with pytest.raises(RuntimeError, match="was invalidated"):
            service.prepare_thinkthread_session(session.agent_session_id)
        with pytest.raises(RuntimeError, match="was invalidated"):
            service.queries.require_native_execution(session.agent_session_id, now=0.0)
        assert domain_files(runtime.root_dir) == before
        return
    assert service.queries.require_native_execution(session.agent_session_id, now=0.0) == session
    before = domain_files(runtime.root_dir)
    prepared = service.prepare_thinkthread_session(session.agent_session_id)
    assert (
        prepared.baseline_snapshot_id
        == runtime.search_run.state.read_run(session.run_id).source_artifact_ref.id
    )
    assert prepared.session == session
    assert prepared.inner_git_binding is None
    assert domain_files(runtime.root_dir) == before


@pytest.mark.parametrize("candidate_host", ["no_budget"], indirect=True)
def test_thinkthread_continuation_needs_no_fixed_worker_duration(candidate_host):
    runtime, service, tasks, session = candidate_host
    continued = service.prepare_thinkthread_continuation(
        run_id=session.run_id, candidate_id=tasks[0].candidate_id,
        agent_session_id=session.agent_session_id,
    )
    assert continued.agent_session_id == session.agent_session_id
    assert continued.message
    assert runtime.candidate_sessions.invocation_timeout(session.agent_session_id, 120) == 120


@pytest.mark.parametrize("mismatch", ["candidate", "run", "feedback"])
def test_continuation_rejects_foreign_binding_before_mutation(candidate_host, mismatch):
    runtime, service, tasks, session = candidate_host
    feedback = None
    if mismatch == "feedback":
        artifact = runtime.search_run.state.read_run(session.run_id).source_artifact_ref
        feedback = CandidateAttemptFeedback(
            run_id=session.run_id,
            candidate_id=tasks[0].candidate_id,
            agent_session_id="agent_foreign",
            attempt_id="attempt_1",
            request_id="request_1",
            attempt_iteration=1,
            attempt_artifact_ref=artifact,
            workspace_artifact_after_attempt=artifact,
            verified_at=session.created_at,
            score_report=ScoreReport(
                run_id=session.run_id,
                candidate_id=tasks[0].candidate_id,
                validity_passed=True,
                process_passed=True,
                verifier_results=[],
                attempt_id="attempt_1",
                attempt_iteration=1,
                attempt_artifact_ref=artifact,
                workspace_artifact_after_settlement=artifact,
            ),
        )
    before = domain_files(runtime.root_dir)
    with pytest.raises((ValueError, FileNotFoundError), match="match|not found"):
        service.prepare_thinkthread_continuation(
            run_id="run_foreign" if mismatch == "run" else session.run_id,
            candidate_id=tasks[1 if mismatch == "candidate" else 0].candidate_id,
            agent_session_id=session.agent_session_id,
            attempt_feedback=feedback,
        )
    assert domain_files(runtime.root_dir) == before


@pytest.mark.parametrize("inner_agent", ["common", "autoresearch"])
def test_candidate_preparation_runs_from_typed_inputs_without_runtime(
    candidate_host, inner_agent
):
    runtime, _, tasks, session = candidate_host
    run = runtime.search_run.state.read_run(session.run_id)
    frozen = runtime.search_run.state.read_frozen_spec(run.frozen_spec_id)
    record = runtime.candidate_creation.state.read_candidate(run.run_id, tasks[0].candidate_id)
    record.task.strategy_metadata["worker_policy"]["inner_agent"] = inner_agent
    record.iterations = [
        IterationRecord(
            iteration=1, score=3.0, process_passed=True, created_at=session.created_at
        )
    ]
    record.inner_agent_state["attempt_git_heads"] = {"1": "a" * 40}
    session.launch["model"] = {"provider": "test-provider", "model": "test-model"}
    saved = record.model_dump(mode="json")
    source = SimpleNamespace(
        read_run=lambda _id: run.model_copy(deep=True),
        read_frozen_spec=lambda _id: frozen.model_copy(deep=True),
        read_agent_session=lambda _id, _run_id=None: session.model_copy(deep=True),
        read_candidate=lambda _run_id, _candidate_id: record.model_copy(deep=True),
        read_candidates=lambda _id: [record.model_copy(deep=True)],
        read_agent_sessions=lambda _id: [],
    )
    def unexpected(*args, **kwargs):
        raise AssertionError("unexpected continuation")

    use_case = ThinkThreadCandidatePreparation(
        CandidateHostQueries(source, context=None),
        continue_session=unexpected,
        resolve_binding=candidate_inner_agent_binding,
    )
    before = domain_files(runtime.root_dir)
    prepared = use_case.prepare_thinkthread_session(session.agent_session_id)
    assert prepared.session == session
    assert prepared.root_thinkthread_id == run.source_provider_handle["root_thinkthread_id"]
    assert prepared.baseline_snapshot_id == run.source_artifact_ref.id
    assert record.model_dump(mode="json") == saved
    assert domain_files(runtime.root_dir) == before
    assert prepared.session.launch["model"] == session.launch["model"]
    binding = prepared.inner_git_binding
    if inner_agent == "common":
        assert binding is None
    else:
        import base64

        assert binding["initial_result_count"] == 1
        assert base64.b64decode(
            binding["initial_results_base64"]
        ).decode().splitlines()[1] == ("a" * 40 + "\t3\tpass\trecovered iteration 1")


@pytest.mark.parametrize("candidate_host", ["adaptive"], indirect=True)
def test_thinkthread_allocation_child_starts_from_selected_snapshot(candidate_host):
    runtime, service, tasks, _session = candidate_host
    run = runtime.search_run.state.read_run(tasks[0].run_id)
    frozen = runtime.search_run.state.read_frozen_spec(run.frozen_spec_id)
    source = runtime.candidate_creation.state.read_candidate(
        run.run_id, tasks[0].candidate_id
    )
    plan = runtime.search_run.state.read_plan(run.run_id, tasks[0].plan_id)
    selected = ArtifactRef(
        kind="fs_snapshot", id="fsnap-adaptive-source", provider="thinkthread"
    )
    expansion_source = ExpansionSource(
        candidate_id=source.candidate_id,
        iteration=1,
        settlement_id="settlement_source",
        node_id=f"{source.candidate_id}/iteration-0001",
        verifier_record_id="record_source",
        attempt_artifact_ref=selected,
        attempt_artifact_hash="a" * 64,
        ledger_artifact_ref=selected,
        allocation_depth=source.task.allocation_depth,
    )

    child = runtime.materialize_candidate(CandidateMaterialization(
        run=run,
        frozen=frozen,
        candidate_id="c003",
        plan=plan,
        proposal=CandidateProposal(
            intent="expand selected snapshot",
            metadata={
                "allocation_decision_id": "allocation_test",
                "expansion_id": "expansion_test",
            },
        ),
        slot=1,
        expansion_source=expansion_source,
        source_record=source,
    ))
    child_session = runtime.start_agent_session(
        run.run_id,
        child.candidate_id,
        {
            "adaptive_allocation_expansion": True,
            "allocation_decision_id": "allocation_test",
            "expansion_id": "expansion_test",
        },
    )
    prepared = service.prepare_thinkthread_session(child_session.agent_session_id)

    assert child.task.base_artifact_ref == selected
    assert child.task.workspace_base_revision == selected.id
    assert child.task.workspace_provider_handle["base_snapshot_id"] == selected.id
    assert child.task.expansion_source == expansion_source
    assert child.task.parent_id == source.candidate_id
    assert child.task.parent_candidate_ids == [source.candidate_id]
    assert child.task.allocation_depth == source.task.allocation_depth + 1
    assert child.task.selected_model == source.task.selected_model
    assert prepared.baseline_snapshot_id == selected.id
    assert not child.task.workspace.exists()


@pytest.mark.parametrize(
    "budget", [{"max_runtime_seconds": 0}, {"max_turns": 0}, {"unknown": 1}]
)
def test_shared_worker_budget_rejects_invalid_overrides(budget):
    with pytest.raises(ValueError):
        normalize_worker_budget_override(worker_budget=budget)


def test_ledger_projection_preserves_settled_and_inherited_entries(candidate_host):
    from goal_plus.application.candidate_ledger import (
        backfill_results_ledger_from_iterations,
        render_thinkthread_inner_git_results_tsv,
    )

    runtime, _, tasks, session = candidate_host
    record = runtime.candidate_creation.state.read_candidate(session.run_id, tasks[0].candidate_id)
    record.results_ledger = [
        ResultLedgerEntry(
            source_run_id="parent",
            source_candidate_id="c099",
            iteration=1,
            metric_name="value",
            score=2.0,
            status="pass",
            hypothesis="inherited",
            git_head="b" * 40,
            created_at=session.created_at,
        )
    ]
    record.iterations = [
        IterationRecord(
            iteration=1, score=3.0, process_passed=True, created_at=session.created_at
        )
    ]
    record.inner_agent_state["attempt_git_heads"] = {"1": "invalid"}
    backfill_results_ledger_from_iterations(record, "value")
    saved = record.model_dump(mode="json")
    backfill_results_ledger_from_iterations(record, "value")
    rendered = render_thinkthread_inner_git_results_tsv(record, "value")
    assert record.model_dump(mode="json") == saved
    assert len(record.results_ledger) == 2
    assert rendered.splitlines()[1].startswith("b" * 40 + "\t2\tpass")
    assert rendered.splitlines()[2].startswith("\t3\tpass")
