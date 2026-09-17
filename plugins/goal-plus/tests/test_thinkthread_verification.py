"""Provider and settlement contracts, independent of native session orchestration."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from goal_plus.infrastructure.verifiers.thinkthread_attempt import ProviderThinkThreadAttempt
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.runtime import FileSearchRuntime
from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError
from tests._runtime_helpers import make_project
from tests._thinkthread_candidates import attempt_parameters, bind_candidate, verify_candidate
from goal_plus.domain.evidence import CandidateAttemptAccepted
from goal_plus.ports.thinkthread_verification import DeferredCandidateAttempt
from goal_plus.tools import SearchTools
from tests._thinkthread_sdk import IntegratedAgentPosix, thinkthread_spec


@pytest.fixture
def prepared_candidates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def prepare(*, inner_agent="common", count=1, resource=None):
        project = make_project(tmp_path)
        sdk = IntegratedAgentPosix(project)
        monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: sdk)
        runtime = FileSearchRuntime(tmp_path.parent / (tmp_path.name + "-runtime"))
        spec = thinkthread_spec(project)
        spec.strategy.inner_agent = inner_agent
        spec.process_verifiers[0].resource_lock = resource
        monkeypatch.setenv("GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR", str(tmp_path / "locks"))
        frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="pi")
        run_id = runtime.create_run(frozen.frozen_spec_id)
        tasks = runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=count).plan_id)
        sessions = [bind_candidate(runtime, sdk, run_id, task.candidate_id) for task in tasks]
        return runtime, sdk, sessions

    return prepare


def candidate_record(runtime, session):
    return runtime.candidate_creation.state.read_candidate(session.run_id, session.candidate_id)


@pytest.mark.parametrize("metrics,failure", [
    ({"combined_score": 0, "outcome": "valid", "error": None, "returncode": 99}, None),
    ({"combined_score": 0, "error": "broken evaluator"}, "VerifierCommandFailed"),
    ({"score": 9}, "MissingNumericMetric"),
])
def test_thinkthread_uses_shared_metric_and_error_contract(prepared_candidates, monkeypatch, metrics, failure):
    runtime, sdk, [session] = prepared_candidates()
    monkeypatch.setattr(
        "goal_plus.infrastructure.verifiers.thinkthread.decode_fs_run_output",
        lambda _result: (json.dumps(metrics), ""),
    )
    report = verify_candidate(runtime, sdk, session)
    ranking = next(result for result in report.verifier_results if result.role == "ranking_signal")
    assert ranking.failure_class == failure
    assert ranking.passed is (failure is None)
    assert ranking.metrics["returncode"] == 0
    assert report.process_passed is (failure is None)


@pytest.mark.parametrize("exit_status,failure", [
    ({"kind": "code", "code": 1}, None),
    ({"kind": "signal", "signal": 15}, "VerifierSignal"),
    ({"kind": "timeout"}, "Timeout"),
])
def test_thinkthread_invalid_baseline_preserves_exit_and_rejects_interruption(
    prepared_candidates, monkeypatch, exit_status, failure,
):
    invoke = IntegratedAgentPosix.invoke

    def classified_output(self, operation, params=None, **kwargs):
        result = invoke(self, operation, params, **kwargs)
        if operation == "fs.run":
            result["exit"] = exit_status
        return result

    monkeypatch.setattr(IntegratedAgentPosix, "invoke", classified_output)
    monkeypatch.setattr(
        "goal_plus.infrastructure.verifiers.thinkthread.decode_fs_run_output",
        lambda _result: (json.dumps({
            "combined_score": -2, "outcome": "invalid", "error": "candidate rejected",
        }), ""),
    )
    if failure:
        with pytest.raises(ValueError, match=failure):
            prepared_candidates()
        return
    runtime, sdk, [session] = prepared_candidates()
    baseline = runtime.search_run.state.read_run(session.run_id).workspace_bootstrap_verification
    assert baseline.evaluation_completed and not baseline.passed
    assert baseline.aggregate_score == -2
    result = baseline.verifier_results[0]
    assert result.failure_class == "CandidateRejected"
    assert result.metrics["returncode"] == 1
    assert result.metrics["error"] == "candidate rejected"
    report = verify_candidate(runtime, sdk, session)
    assert not report.process_passed
    assert report.verifier_results[0].failure_class == "CandidateRejected"


def test_common_verifier_queries_exact_tt_attempt_without_resubmitting(prepared_candidates):
    runtime, sdk, [session] = prepared_candidates()
    params = attempt_parameters(runtime, session)
    submission = DeferredCandidateAttempt(**{key: value for key, value in params.items() if key != "hypothesis"},
                                          request_id="request-1")
    tools = SearchTools(runtime, candidate_attempt=submission)
    args = {key: params[key] for key in ("run_id", "candidate_id", "agent_session_id")}
    accepted = tools.goal_plus_search_run_verifier(**args, hypothesis=params["hypothesis"])
    assert accepted["candidate_action"] == "end_turn"
    tools = SearchTools(runtime, candidate_attempt=replace(
        submission, prior=CandidateAttemptAccepted.model_validate(accepted),
    ))
    before = candidate_record(runtime, session)
    operations = list(sdk.operations)
    assert tools.goal_plus_search_run_verifier(**args, attempt_id=accepted["attempt_id"]) == accepted
    with pytest.raises(ValueError, match="VerifierAttemptUnavailable"):
        tools.goal_plus_search_run_verifier(**args, attempt_id="unknown")
    assert candidate_record(runtime, session) == before
    assert sdk.operations == operations
    report = verify_candidate(runtime, sdk, session)
    assert tools.goal_plus_search_run_verifier(**args, attempt_id=report.attempt_id) == report.model_dump(mode="json")
    assert len(candidate_record(runtime, session).iterations) == 1


@pytest.mark.parametrize("mismatch", ["child", "snapshot", "inner_git"])
def test_exact_provider_facts_are_rechecked_before_settlement(prepared_candidates, monkeypatch, mismatch):
    runtime, sdk, [session] = prepared_candidates(inner_agent="autoresearch")
    method = {"child": "read_child", "snapshot": "capture", "inner_git": "settle_inner_git"}[mismatch]
    original = getattr(ProviderThinkThreadAttempt, method)

    def wrong_facts(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if mismatch == "child":
            return replace(result, parent_thinkthread_id="tt-other-root")
        if mismatch == "snapshot":
            return replace(result, branch_id="fsbranch-other")
        return replace(result, settlement=replace(result.settlement, baseline_git_head="c" * 40))

    monkeypatch.setattr(ProviderThinkThreadAttempt, method, wrong_facts)
    before = candidate_record(runtime, session)
    with pytest.raises((PermissionError, RuntimeError), match="mismatch"):
        verify_candidate(runtime, sdk, session)
    rejected = candidate_record(runtime, session)
    for field in ("iterations", "results_ledger", "score_report", "inner_agent_state"):
        assert getattr(rejected, field) == getattr(before, field)
    monkeypatch.setattr(ProviderThinkThreadAttempt, method, original)
    assert verify_candidate(runtime, sdk, session).process_passed
    assert len(candidate_record(runtime, session).iterations) == 1
    assert sum(op == "fs.branch.snapshot" for op, _ in sdk.operations) == 1
    assert sum(op == "fs.run" and p.get("writes") == "snapshot" for op, p in sdk.operations) == 1


@pytest.mark.parametrize("operation", ["fs.branch.snapshot", "fs.run", "inner_git_settlement"])
def test_unknown_provider_result_recovers_without_duplicate_mutation(prepared_candidates, operation):
    runtime, sdk, [session] = prepared_candidates(
        inner_agent="autoresearch" if operation == "inner_git_settlement" else "common",
    )
    if operation == "inner_git_settlement":
        sdk.inner_git_settlement_completion_unknown_once = True
        sdk.request_status_completion_unknown_once.add("fs.run")
    else:
        sdk.completion_unknown_once.add(operation)
        sdk.request_status_completion_unknown_once.add(operation)
    with pytest.raises((AgentPosixBridgeError, RuntimeError)):
        verify_candidate(runtime, sdk, session)
    assert candidate_record(runtime, session).iterations == []
    report = verify_candidate(runtime, sdk, session)
    assert report.process_passed
    replayed = verify_candidate(runtime, sdk, session)
    assert replayed == report
    record = candidate_record(runtime, session)
    assert len(record.iterations) == len(record.results_ledger) == 1
    assert record.iterations[0].metrics["_candidate_attempt"]["id"] == report.attempt_id
    mutation_kind = {"fs.branch.snapshot": "branch_snapshot", "fs.run": "verifier_run",
                     "inner_git_settlement": "inner_git_settlement"}[operation]
    mutation = next(item for item in record.provider_mutations if item.operation == mutation_kind)
    assert mutation.state == "closed"
    calls = [params for op, params in sdk.operations
             if op == ("fs.run" if operation == "inner_git_settlement" else operation)
             and params.get("requestId") == mutation.request_id]
    assert len(calls) == 1
    if operation == "inner_git_settlement":
        assert record.inner_agent_state["result_count"] == 1


def test_request_close_recovery_preserves_evidence_without_repeating_verifier(prepared_candidates):
    runtime, sdk, [session] = prepared_candidates()
    sdk.close_failure_once.add("fs.run")
    report = verify_candidate(runtime, sdk, session)
    record = candidate_record(runtime, session)
    mutation = next(item for item in record.provider_mutations if item.operation == "verifier_run")
    assert mutation.state == "succeeded"
    assert mutation.close_state == "needs_recovery"
    before = [item for item in sdk.operations if item[0] == "fs.run"]
    assert runtime.reconcile_thinkthread_request_closes(session.run_id, client=sdk) == {
        "closed": [mutation.request_id], "needs_recovery": [],
    }
    recovered = candidate_record(runtime, session)
    assert recovered.score_report == report
    assert recovered.iterations == record.iterations
    assert [item for item in sdk.operations if item[0] == "fs.run"] == before
    assert next(item for item in recovered.provider_mutations if item.request_id == mutation.request_id).state == "closed"


def test_committed_attempt_reconciles_projections_without_provider_calls(prepared_candidates, monkeypatch):
    runtime, sdk, [session] = prepared_candidates()

    def interrupt_projection(run):
        raise OSError("run projection interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(runtime.settlement, "persist_run", interrupt_projection)
        with pytest.raises(OSError, match="run projection interrupted"):
            verify_candidate(runtime, sdk, session)
    record = candidate_record(runtime, session)
    operations = list(sdk.operations)
    assert len(record.iterations) == 1
    assert runtime.search_run.state.read_run(session.run_id).candidates_evaluated == 0
    for _ in range(2):
        assert verify_candidate(runtime, sdk, session) == record.score_report
    assert runtime.search_run.state.read_run(session.run_id).candidates_evaluated == 1
    assert candidate_record(runtime, session).iterations == record.iterations
    assert sdk.operations == operations


def test_definitive_verifier_failure_records_one_failed_attempt(prepared_candidates):
    runtime, sdk, [session, second] = prepared_candidates(count=2, resource="device")
    sdk.definitive_failure_once.add("fs.run")
    report = verify_candidate(runtime, sdk, session)
    assert report.process_passed is False
    assert report.disposition == "failure"
    verifier = report.verifier_results[0]
    assert verifier.failure_class == "VerifierInfrastructureFailure"
    assert verifier.metrics["error_code"] == "FsRunRejected"
    record = candidate_record(runtime, session)
    assert len(record.iterations) == 1
    assert record.iterations[0].failure_class == "VerifierInfrastructureFailure"
    assert verify_candidate(runtime, sdk, second).process_passed


@pytest.mark.parametrize("shared", [False, True])
def test_verifier_execution_coordinates_resources(prepared_candidates, monkeypatch, shared):
    from goal_plus.bootstrap import build_thinkthread_verifiers

    runtime, sdk, sessions = prepared_candidates(count=2)
    run = runtime.search_run.state.read_run(sessions[0].run_id)
    frozen = runtime.search_run.state.read_frozen_spec(run.frozen_spec_id)
    _, verifiers = build_thinkthread_verifiers(runtime.root_dir, runtime.verifiers)
    first_entered, second_entered, release = threading.Event(), threading.Event(), threading.Event()
    original = sdk.invoke
    calls = []

    def execute(method, params=None, **kwargs):
        if method == "fs.run":
            calls.append(params["requestId"])
            if len(calls) == 1:
                first_entered.set()
                assert release.wait(10)
            else:
                second_entered.set()
        return original(method, params, **kwargs)

    monkeypatch.setattr(sdk, "invoke", execute)

    def verify(index):
        command = frozen.spec.process_verifiers[0].model_copy(update={
            "resource_lock": "device" if shared or index == 0 else "another-device",
        })
        return verifiers.run_thinkthread_command(
            client=sdk, run=run, frozen=frozen, candidate_id=sessions[index].candidate_id,
            artifact_ref=run.source_artifact_ref, command=command, phase="candidate",
            iteration=1, command_index=0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(verify, 0)
        try:
            assert first_entered.wait(10)
            second = executor.submit(verify, 1)
            assert second_entered.wait(0.1 if shared else 10) is (not shared)
        finally:
            release.set()
        assert first.result(timeout=10).passed
        assert second.result(timeout=10).passed
    assert len(set(calls)) == 2


def test_unknown_verifier_holds_resource_until_exact_request_recovers(prepared_candidates):
    from goal_plus.infrastructure.state.resource_locks import verifier_resource_lock

    runtime, sdk, [first, second] = prepared_candidates(count=2, resource="device")
    sdk.completion_unknown_once.add("fs.run")
    sdk.request_status_completion_unknown_once.add("fs.run")
    with pytest.raises((AgentPosixBridgeError, RuntimeError)):
        verify_candidate(runtime, sdk, first)
    before = [params for op, params in sdk.operations if op == "fs.run"]
    with pytest.raises(RuntimeError, match="VerifierResourceNeedsRecovery"):
        verify_candidate(runtime, sdk, second)
    with pytest.raises(RuntimeError, match="VerifierResourceNeedsRecovery"):
        with verifier_resource_lock("device"):
            pytest.fail("local verifier entered an unresolved remote resource")
    assert [params for op, params in sdk.operations if op == "fs.run"] == before
    assert verify_candidate(runtime, sdk, first).process_passed
    assert verify_candidate(runtime, sdk, second).process_passed
    with verifier_resource_lock("device"):
        pass


def test_pending_candidate_request_does_not_block_another_candidate(prepared_candidates):
    runtime, sdk, [first, second] = prepared_candidates(count=2)
    sdk.completion_unknown_once.add("fs.branch.snapshot")
    sdk.request_status_completion_unknown_once.add("fs.branch.snapshot")
    with pytest.raises((AgentPosixBridgeError, RuntimeError)):
        verify_candidate(runtime, sdk, first)
    mutation = next(item for item in candidate_record(runtime, first).provider_mutations
                    if item.operation == "branch_snapshot")
    pending = sdk.request_status[mutation.request_id]
    result = pending.pop("result")
    pending["state"] = "accepted"
    assert verify_candidate(runtime, sdk, second).process_passed
    assert candidate_record(runtime, first).iterations == []
    pending.update(state="succeeded", result=result)
    assert verify_candidate(runtime, sdk, first).process_passed
    assert len(candidate_record(runtime, first).iterations) == 1


def test_failed_snapshot_recovery_does_not_create_evidence(prepared_candidates):
    runtime, sdk, [session] = prepared_candidates()
    sdk.completion_unknown_once.add("fs.branch.snapshot")
    sdk.request_status_completion_unknown_once.add("fs.branch.snapshot")
    with pytest.raises((AgentPosixBridgeError, RuntimeError)):
        verify_candidate(runtime, sdk, session)
    mutation = next(item for item in candidate_record(runtime, session).provider_mutations
                    if item.operation == "branch_snapshot")
    sdk.request_status[mutation.request_id] = {
        "requestId": mutation.request_id, "method": "fs.branch.snapshot", "state": "failed",
        "error": {"code": "FsSnapshotFailed", "message": "snapshot failed", "retryable": False},
    }
    with pytest.raises(AgentPosixBridgeError) as failed:
        verify_candidate(runtime, sdk, session)
    assert failed.value.code == "FsSnapshotFailed"
    assert candidate_record(runtime, session).iterations == []
    assert sum(op == "fs.branch.snapshot" for op, _ in sdk.operations) == 1


def test_inner_git_ledger_uses_distinct_attempt_and_settlement_snapshots(prepared_candidates):
    runtime, sdk, [session] = prepared_candidates(inner_agent="autoresearch")
    sdk.forced_verifier_scores = [5.0, 1.0]
    first = verify_candidate(runtime, sdk, session)
    assert first.attempt_artifact_ref.id == "fsnap-2"
    assert first.workspace_artifact_after_settlement.id == "fsnap-3"
    first_record = candidate_record(runtime, session)
    assert first_record.results_ledger_artifact_ref.id == "fsnap-3"
    projected = goal_plus_monitor_snapshot(runtime.root_dir, run_id=session.run_id)["candidates"][session.candidate_id]["results_tsv"]
    assert projected["path"] == "results.tsv"
    assert projected["scope"] == "thinkthread_child_private"
    assert projected["exists"] is True
    assert projected["last_updated_at"] is None
    assert projected["size_bytes"] is None
    assert projected["row_count"] == 1
    second = verify_candidate(runtime, sdk, session, iteration=2)
    assert second.disposition == "discard"
    assert second.attempt_artifact_ref.id == "fsnap-4"
    assert second.workspace_artifact_after_settlement.id == "fsnap-5"
    record = candidate_record(runtime, session)
    assert [item.artifact_ref.id for item in record.iterations] == ["fsnap-2", "fsnap-4"]
    assert [item.ledger_artifact_ref.id for item in record.iterations] == ["fsnap-3", "fsnap-5"]
    assert [item.disposition for item in record.iterations] == ["keep", "discard"]
    assert all(item.git_head is None and item.ledger_git_head is None for item in record.iterations)
    assert all(item.git_head is None for item in record.results_ledger)
    assert record.results_ledger_artifact_ref.id == "fsnap-5"
    assert record.inner_agent_state["result_count"] == 2
    assert record.inner_agent_state["attempt_git_heads"] == {"1": f"{1:040x}", "2": f"{2:040x}"}
    assert set(runtime.artifact_lifecycle.owned_snapshots(session.run_id)) == {
        "fsnap-S0", "fsnap-2", "fsnap-3", "fsnap-4", "fsnap-5",
    }
    settlements = [params for op, params in sdk.operations if op == "fs.run" and params.get("writes") == "snapshot"]
    assert len(settlements) == 2
    assert "--restore-git-head" not in settlements[0]["invocation"]["argv"]
    argv = settlements[1]["invocation"]["argv"]
    assert argv[argv.index("--restore-git-head") + 1] == f"{1:040x}"
