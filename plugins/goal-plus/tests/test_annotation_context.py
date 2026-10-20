"""Exact context behavior with in-memory state and controlled artifact results."""

from dataclasses import replace
from pathlib import Path

import pytest

from goal_plus.application.annotation import AnnotationWorkflow
from goal_plus.application.annotation_admission import AnnotationAdmission
from goal_plus.application.annotation_context import AnnotationContextBuilder
from goal_plus.application.search_context import SearchContext
from goal_plus.domain.artifacts import ArtifactRef, ReferenceWorkspaceAccess
from goal_plus.ports.annotation_context import (
    MAX_EVIDENCE_ANNOTATION_DIFF_BYTES,
    MAX_EVIDENCE_PEER_DIFF_BYTES,
    TOOL_VIEW_MAX_CONTENT_BYTES,
    AnnotationCandidateContext,
    AnnotationContext,
)
from goal_plus.ports.artifacts import ArtifactDiff, ArtifactReadError
from goal_plus.ports.state import ProviderResourceCleanupRecord
from tests.test_annotation_admission import AdmissionState
from tests.test_annotation_workflow import Clock, tool


class ContextState(AdmissionState):
    def __init__(self, clock):
        super().__init__(clock)
        self.iteration.hypothesis = "Change the measured implementation"
        self.iteration.attempt_changed_files = ["main.py"]
        self.iteration.disposition = "keep"
        self.records = [
            AnnotationCandidateContext(
                "c001",
                "git_worktree",
                [self.iteration],
                Path("/not-a-live-workspace"),
                "baseline",
                ArtifactRef(kind="git_commit", provider="git_worktree", id="baseline"),
            )
        ]
        self.tool_budgets = []

    def read_context_candidate(self, run_id, candidate_id):
        return next(
            r
            for r in self.read_candidate_evidence(run_id)
            if r.candidate_id == candidate_id
        )

    def read_context_candidates(self, run_id):
        return self.read_candidate_evidence(run_id)

    def tool_view_input(self, run_id, item, *, max_content_bytes):
        assert run_id == self.run.run_id
        self.tool_budgets.append(max_content_bytes)
        return {"tool_id": item.tool_id, "content": "bounded"}, max_content_bytes


class Artifacts:
    def __init__(self):
        self.available = True
        self.requests = []
        self.opened = []
        self.truncated = False
        self.fail_index = set()
        self.fail_diff = set()

    def commit_available(self, workspace, commit):
        return self.available

    def changed_files(self, comparison):
        if comparison.head.id in self.fail_index:
            raise ArtifactReadError("RuntimeError: unavailable index")
        return ["main.py"]

    def diff(self, comparison, *, paths, max_bytes, function_context):
        self.requests.append((comparison, paths, max_bytes, function_context))
        if comparison.head.id in self.fail_diff:
            raise ArtifactReadError("RuntimeError: bounded diff unavailable")
        return ArtifactDiff(
            f"{comparison.base.id} -> {comparison.head.id}", self.truncated
        )

    def open_view(self, workspace, artifact, destination, *, workspace_provider):
        self.opened.append((workspace, artifact, destination, workspace_provider))
        return ReferenceWorkspaceAccess(
            kind="filesystem_path",
            root=destination,
            readonly_contract="soft_detected",
        )


@pytest.fixture
def context_api():
    clock = Clock()
    state = ContextState(clock)
    workflow = AnnotationWorkflow(tasks=state, evidence=state, tools=state, clock=clock)
    context = SearchContext(state)
    admission = AnnotationAdmission(workflow=workflow, source=state, context=context)
    artifacts = Artifacts()
    builder = AnnotationContextBuilder(
        workflow=workflow,
        source=state,
        admission_source=state,
        context=context,
        artifacts=artifacts,
        root_dir=Path("/not-a-runtime-directory"),
    )
    return builder, admission, state, artifacts, clock


def create(admission):
    with admission.tasks.lock_run("run_test"):
        return admission.create("run_test", "c001", 1)


def build(builder):
    return builder.build("run_test", "c001", 1)


def test_exact_context_uses_settled_anchors_and_preserves_state(context_api):
    builder, admission, state, artifacts, _ = context_api
    task = create(admission)
    before = (state.run.model_dump(), state.iteration.model_dump(), task.model_dump())
    result = build(builder)
    assert set(result) == set(AnnotationContext.__required_keys__)
    assert result["actual_diff"] == "base -> attempt"
    assert result["candidate_diff"] == "baseline -> attempt"
    assert (
        result["exact_attempt_artifact_ref"] == task.attempt_artifact_ref.model_dump()
    )
    assert result["task_context"] == state.frozen.spec.objective
    assert result["artifact_views"][0]["access"]["root"].startswith(
        "/not-a-runtime-directory/runs/run_test/"
    )
    assert all(
        r[2:] == (MAX_EVIDENCE_ANNOTATION_DIFF_BYTES, True) for r in artifacts.requests
    )
    assert (
        state.run.model_dump(),
        state.iteration.model_dump(),
        create(admission).model_dump(),
    ) == before


def test_annotation_uses_settled_spec_version_after_branch_revision(context_api, monkeypatch):
    builder, admission, state, _, _ = context_api
    original = state.frozen.model_copy(deep=True)
    revised = original.model_copy(deep=True, update={
        "frozen_spec_id": "spec_revised", "spec_hash": "b" * 64,
    })
    revised.spec.objective = "Measure the revised branch contract"
    revised.spec.process_verifiers[0].command = ["python", "verify.py", "--revised"]
    versions = {original.frozen_spec_id: original, revised.frozen_spec_id: revised}
    monkeypatch.setattr(state, "read_frozen_spec", lambda identity: versions[identity].model_copy(deep=True))
    state.iteration.frozen_spec_id = revised.frozen_spec_id
    state.iteration.spec_hash = revised.spec_hash
    task = create(admission)
    context = build(builder)
    assert context["objective"] == "Measure the revised branch contract"
    assert context["verifier_contract"][0]["command"] == ["python", "verify.py", "--revised"]
    assert context["frozen_spec_id"] == task.frozen_spec_id == revised.frozen_spec_id
    assert context["spec_hash"] == task.spec_hash == revised.spec_hash
    assert state.run.frozen_spec_id == original.frozen_spec_id


@pytest.mark.parametrize(
    "mutation",
    [
        "worker",
        "commit",
        "base",
        "artifact",
        "files",
        "profile",
        "missing",
        "context",
        "unavailable",
    ],
)
def test_invalid_context_is_rejected_without_publication(context_api, mutation):
    builder, admission, state, artifacts, _ = context_api
    create(admission)
    if mutation == "worker":
        state.iteration.agent_session_id = None
    elif mutation == "commit":
        state.iteration.git_head = "other"
    elif mutation == "base":
        state.iteration.attempt_base_git_head = "other"
    elif mutation == "artifact":
        state.iteration.artifact_ref = ArtifactRef(
            kind="git_commit", id="other", provider="git_worktree"
        )
    elif mutation == "files":
        state.iteration.attempt_changed_files = ["other.py"]
    elif mutation == "profile":
        state.task_records[("c001", 1)].profile = None
    elif mutation == "missing":
        state.task_records.clear()
    elif mutation == "context":
        state.frozen.spec.objective = "Changed objective"
    else:
        artifacts.available = False
    before = (
        state.run.model_dump(),
        state.iteration.model_dump(),
        dict(state.task_records),
    )
    with pytest.raises(RuntimeError):
        build(builder)
    assert not artifacts.opened
    assert (
        state.run.model_dump(),
        state.iteration.model_dump(),
        state.task_records,
    ) == before


@pytest.mark.parametrize("truncated", [False, True])
def test_target_diff_failure_is_an_index_omission_not_a_partial_diff(
    context_api, truncated
):
    builder, admission, _, artifacts, _ = context_api
    create(admission)
    artifacts.truncated = truncated
    if not truncated:
        artifacts.fail_diff.add("attempt")
    result = build(builder)
    assert result["actual_diff"] == result["candidate_diff"] == ""
    assert result["actual_diff_omitted"] and result["candidate_diff_omitted"]
    assert len(result["artifact_views"]) == 1


def test_target_index_failure_is_fatal(context_api):
    builder, admission, _, artifacts, _ = context_api
    create(admission)
    artifacts.fail_index.add("attempt")
    with pytest.raises(ArtifactReadError, match="index"):
        build(builder)
    assert not artifacts.opened


@pytest.mark.parametrize("failure", ["index", "diff", "truncated", "none"])
def test_frozen_peer_reads_are_best_effort_and_use_the_peer_budget(
    context_api, failure
):
    builder, admission, state, artifacts, _ = context_api
    state.frozen.supplemental_evaluation_enabled = True
    peer = state.iteration.model_copy(
        deep=True,
        update={
            "git_head": "peer",
            "settlement_id": "peer-settlement",
            "artifact_ref": ArtifactRef(
                kind="git_commit", provider="git_worktree", id="peer"
            ),
        },
    )
    state.records.append(
        replace(state.records[0], candidate_id="c002", iterations=[peer])
    )
    task = create(admission)
    if failure == "index":
        artifacts.fail_index.add("peer")
    elif failure == "diff":
        artifacts.fail_diff.add("peer")
    elif failure == "truncated":
        artifacts.truncated = True
    peers = builder.comparison_peers("run_test", comparison_basis=task.comparison_basis)
    assert peers[0]["artifact_ref"]["id"] == "peer"
    if failure == "none":
        assert peers[0]["candidate_diff"] == "baseline -> peer"
        assert peers[0]["diff_omitted"] is None
    else:
        assert peers[0]["candidate_diff"] is None
        assert peers[0]["diff_omitted"]
    if failure != "index":
        assert artifacts.requests[-1][2:] == (MAX_EVIDENCE_PEER_DIFF_BYTES, False)


def test_shared_tool_content_uses_one_budget_and_prior_goal_evidence(context_api):
    builder, admission, state, _, clock = context_api
    first = tool(state, clock)
    second = first.model_copy(update={"tool_id": "tool_2"})
    state.iteration.shared_tools.append(second)
    state.pending_ids.add(second.tool_id)
    create(admission)
    result = build(builder)
    assert state.tool_budgets == [TOOL_VIEW_MAX_CONTENT_BYTES, 0]
    assert [item["tool_id"] for item in result["published_tools"]] == [
        first.tool_id,
        second.tool_id,
    ]
    assert all(
        item["goal_evidence"]["baseline_score"] is None
        for item in result["published_tools"]
    )
    assert all(
        item["goal_evidence"]["goal_effect"] == "unknown"
        for item in result["published_tools"]
    )


@pytest.mark.parametrize(
    "status", ["removed", "already_absent", "prepared", "needs_recovery", "failed"]
)
def test_cleanup_fence_prevents_opening_artifacts(context_api, status):
    builder, admission, state, artifacts, clock = context_api
    task = create(admission)
    snapshot = ArtifactRef(
        kind="fs_snapshot", provider="thinkthread", id="fsnap-attempt"
    )
    state.iteration.artifact_ref = snapshot
    task.artifact_views[0].artifact_ref = snapshot
    task.artifact_views[0].workspace_provider = "thinkthread"
    state.run.provider_cleanup = [
        ProviderResourceCleanupRecord(
            resource_id=snapshot.id,
            state=status,
            request_id="req-remove",
            created_at=clock.timestamp(),
            updated_at=clock.timestamp(),
        )
    ]
    with pytest.raises(
        RuntimeError, match="removed after terminal cleanup|availability"
    ):
        builder.open_views(task)
    assert not artifacts.opened


def test_artifact_view_reference_must_match_persisted_evidence(context_api):
    builder, admission, _, artifacts, _ = context_api
    task = create(admission)
    task.artifact_views[0].iteration = 2
    with pytest.raises(RuntimeError, match="Artifact View Evidence"):
        builder.open_views(task)
    assert not artifacts.opened
