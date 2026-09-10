
from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus.domain.evidence import CandidateAttemptFeedback, ScoreReport
from goal_plus.host_services import SearchHostService
from goal_plus.models import SearchSpec
from goal_plus.runtime import FileSearchRuntime
from goal_plus.agent_hosts import candidate_inner_agent_binding
from goal_plus.application.candidate_host import ThinkThreadCandidatePreparation
from goal_plus.application.candidate_queries import CandidateHostQueries
from goal_plus.domain.evidence import IterationRecord, ResultLedgerEntry
from goal_plus.domain.worker_policy import normalize_worker_budget_override
from tests._runtime_helpers import make_project, spec_for
from tests.test_thinkthread_integration import IntegratedAgentPosix, thinkthread_spec


@pytest.fixture
def candidate_host(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    session = runtime.start_agent_session(run_id, tasks[0].candidate_id)
    session = runtime.bind_agent_handle(
        session.agent_session_id,
        {"host": "thinkthread", "external_id": "tt-child-test"},
    )
    return runtime, SearchHostService(runtime.root_dir, runtime=runtime), tasks, session


def domain_files(root: Path):
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.json")
    }


@pytest.mark.parametrize(
    "recorded_role,requested_role",
    [("default", None), ("default", "default"), ("default", "explorer"),
     ("explorer", None), ("explorer", "explorer")],
)
def test_codex_launch_default_role_semantics(tmp_path, recorded_role, requested_role):
    project = make_project(tmp_path)
    spec = spec_for(project, max_parallel=1)
    spec.strategy.worker_agent_type = recorded_role
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], native_host="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    queries = SearchHostService(runtime.root_dir, runtime=runtime).queries
    spawn_input = dict(session.launch)
    if requested_role is None:
        spawn_input.pop("agent_type")
    else:
        spawn_input["agent_type"] = requested_role
    before = domain_files(runtime.root_dir)
    if (requested_role or "default") == recorded_role:
        assert queries.require_native_execution(
            session.agent_session_id, now=0.0, spawn_input=spawn_input,
        ) == session
    else:
        with pytest.raises(RuntimeError, match="native launch contract mismatch: agent_type"):
            queries.require_native_execution(
                session.agent_session_id, now=0.0, spawn_input=spawn_input,
            )
    assert domain_files(runtime.root_dir) == before


def test_native_wake_rejects_pending_allocation_retirement(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["workspace"] = {"backend": "git_worktree"}
    spec_data["strategy"] = {
        "name": "random",
        "orchestration_mode": "adaptive_search",
        "selection": {
            "ranking_keys": ["quality_score", "hard_score"],
            "ranking_tolerance": [0.05, 0.0],
        },
        "allocation": {"policy": "priority_replace/v1"},
    }
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
        native_host="codex",
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

    with pytest.raises(RuntimeError, match="pending allocation decision"):
        queries.require_native_execution(session.agent_session_id, now=0.0)


@pytest.mark.parametrize("invalidated", [False, True])
def test_prepare_dispatch_typed_payload(candidate_host, invalidated):
    runtime, service, tasks, session = candidate_host
    if invalidated:
        run = runtime.search_run.state.read_run(session.run_id)
        run.invalidated_at = session.created_at
        run.invalidation_reason = "verifier_target_mismatch"
        runtime.search_run.persist(run)
        before = domain_files(runtime.root_dir)
        with pytest.raises(RuntimeError, match="was invalidated"):
            service.prepare_thinkthread_pool_dispatch(run_id=session.run_id)
        with pytest.raises(RuntimeError, match="was invalidated"):
            service.queries.require_native_execution(session.agent_session_id, now=0.0)
        assert domain_files(runtime.root_dir) == before
        return
    assert service.queries.require_native_execution(session.agent_session_id, now=0.0) == session
    dispatch = service.prepare_thinkthread_pool_dispatch(
        run_id=session.run_id,
        candidate_ids=[tasks[0].candidate_id],
        worker_budgets={tasks[0].candidate_id: {"max_runtime_seconds": 90}},
        max_parallel=1,
    )
    assert (
        dispatch.baseline_snapshot_id
        == runtime.search_run.state.read_run(session.run_id).source_artifact_ref.id
    )
    assert dispatch.max_parallel == 1
    assert len(dispatch.candidates) == 1
    payload = dispatch.candidates[0].pool_payload()
    assert payload["candidate_id"] == tasks[0].candidate_id
    assert payload["agent_session_id"] == session.agent_session_id
    assert payload["message"]
    assert payload["max_runtime_seconds"] == 90
    assert payload["inner_git_binding"] is None


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
    saved = record.model_dump(mode="json")
    source = SimpleNamespace(
        read_run=lambda _id: run.model_copy(deep=True),
        read_frozen_spec=lambda _id: frozen.model_copy(deep=True),
        read_candidates=lambda _id: [record.model_copy(deep=True)],
        read_agent_sessions=lambda _id: [],
    )
    calls = []

    def start(run_id, candidate_id, **_kwargs):
        calls.append((run_id, candidate_id))
        return session.model_copy(deep=True)

    def unexpected(*args, **kwargs):
        raise AssertionError("unexpected continuation")

    use_case = ThinkThreadCandidatePreparation(
        CandidateHostQueries(source, context=None),
        start_session=start,
        continue_session=unexpected,
        resolve_binding=candidate_inner_agent_binding,
    )
    dispatch = use_case.prepare_thinkthread_pool_dispatch(run_id=run.run_id)
    assert calls == [(run.run_id, record.candidate_id)]
    assert record.model_dump(mode="json") == saved
    binding = dispatch.candidates[0].inner_git_binding
    if inner_agent == "common":
        assert binding is None
    else:
        import base64

        assert binding["initial_result_count"] == 1
        assert base64.b64decode(
            binding["initial_results_base64"]
        ).decode().splitlines()[1] == ("a" * 40 + "\t3\tpass\trecovered iteration 1")
    calls.clear()
    with pytest.raises(ValueError, match="unknown ThinkThread candidates"):
        use_case.prepare_thinkthread_pool_dispatch(
            run_id=run.run_id, candidate_ids=["foreign"]
        )
    assert calls == []


@pytest.mark.parametrize("host", ["codex", "pi-rpc", "thinkthread"])
@pytest.mark.parametrize(
    "budget", [{"max_runtime_seconds": 0}, {"max_turns": 2}, {"unknown": 1}]
)
def test_shared_worker_budget_rejects_invalid_overrides(host, budget):
    with pytest.raises(ValueError):
        normalize_worker_budget_override(worker_host=host, worker_budget=budget)


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
