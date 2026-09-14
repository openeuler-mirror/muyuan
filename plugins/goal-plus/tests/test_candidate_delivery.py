"""Shared Evidence delivery policy; native host protocol is tested separately."""

from pathlib import Path

from goal_plus.application.candidate_delivery import CandidateDelivery
from goal_plus.candidate_host import candidate_host_queries
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, spec_for


def candidate_delivery(tmp_path: Path, *, failing_verifier: bool = False):
    project = make_project(tmp_path)
    if failing_verifier:
        (project / "evaluator.py").write_text(
            "import json, os\nfrom pathlib import Path\n"
            "if os.environ['GOAL_PLUS_VERIFIER_PHASE'] == 'candidate':\n"
            "    Path('unexpected-output.bin').write_text('generated')\n"
            "print(json.dumps({'combined_score': 1.0}))\n"
        )
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=1), [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.candidate_sessions.prepare_invocation(run_id, task.candidate_id, agent_harness="codex")
    review = CandidateDelivery(runtime.queries, candidate_host_queries(runtime.root_dir))
    return runtime, task, session, review


def test_delivery_requests_verification_only_for_unverified_changes(tmp_path):
    runtime, task, session, delivery = candidate_delivery(tmp_path)
    assert delivery.review(task.run_id, task.candidate_id).action == "unchanged"
    (task.workspace / "initial_program.py").write_text("VALUE = 7\n")
    assert delivery.review(task.run_id, task.candidate_id).action == "verify"
    assert delivery.review(task.run_id, task.candidate_id, final_verify=False).action == "disabled"
    assert runtime.list_iterations(task.run_id, task.candidate_id) == []


def test_delivery_reuses_exact_evidence_without_repeating_verification(tmp_path):
    runtime, task, session, delivery = candidate_delivery(tmp_path)
    (task.workspace / "initial_program.py").write_text("VALUE = 7\n")
    report = runtime.run_verifier(
        task.run_id, task.candidate_id, agent_session_id=session.agent_session_id,
        hypothesis="change VALUE",
    )
    before = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
    review = delivery.review(task.run_id, task.candidate_id)
    assert review.action == "reuse"
    assert review.report["aggregate_score"] == report.aggregate_score
    assert review.report["evidence_agent_session_id"] == session.agent_session_id
    assert runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id) == before
    (task.workspace / "initial_program.py").write_text("VALUE = 8\n")
    assert delivery.review(task.run_id, task.candidate_id).action == "verify"


def test_delivery_preserves_infrastructure_failure_and_does_not_retry(tmp_path):
    runtime, task, session, delivery = candidate_delivery(tmp_path, failing_verifier=True)
    (task.workspace / "initial_program.py").write_text("VALUE = 7\n")
    report = runtime.run_verifier(
        task.run_id, task.candidate_id, agent_session_id=session.agent_session_id,
        hypothesis="reproduce verifier side effect",
    )
    assert report.verifier_results[0].failure_class == "VerifierWorkspaceSideEffect"
    before = runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id)
    for final_verify in (True, False):
        review = delivery.review(task.run_id, task.candidate_id, final_verify=final_verify)
        assert review.infrastructure_failure
        assert review.report["failure_class"] == "VerifierWorkspaceSideEffect"
    assert runtime.candidate_creation.state.read_candidate(task.run_id, task.candidate_id) == before
