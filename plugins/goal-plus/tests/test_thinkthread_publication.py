from __future__ import annotations

from goal_plus.infrastructure.state.device_leases import FileDeviceLeases

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from goal_plus.models import (
    ArtifactRef,
    EvidenceAnnotationTask,
    EvidenceArtifactViewReference,
    IterationRecord,
    PromotionRecord,
    PublicationOutcome,
    RunRecord,
    RunState,
)
from goal_plus.runtime import FileSearchRuntime
from goal_plus.domain.serialization import canonical_json
from goal_plus.infrastructure.clock import utc_timestamp
from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError


class StatefulApplyClient:
    def __init__(self) -> None:
        self.base = {
            "project/program.py": b"old\n",
            "project/config.json": b'{"version":1}\n',
            "notes.txt": b"baseline\n",
        }
        self.target = {
            **self.base,
            "project/program.py": b"candidate\n",
        }
        self.root = dict(self.base)
        self.requests: dict[str, dict[str, Any]] = {}
        self.apply_calls = 0
        self.policy_rejected = False
        self.needs_recovery = False
        self.lose_response = False
        self.removed_snapshots: list[str] = []
        self.close_calls = 0
        self.closed_requests: set[str] = set()
        self.close_completion_unknown_before_once = False
        self.close_completion_unknown_after_once = False
        self.close_failure_once = False

    def preflight(self) -> dict[str, Any]:
        return {"controlProtocolVersion": 2}

    @staticmethod
    def _error(code: str, message: str) -> AgentPosixBridgeError:
        return AgentPosixBridgeError(
            message,
            error={
                "rejection": {
                    "error": {
                        "code": code,
                        "message": message,
                        "retryable": False,
                    }
                }
            },
        )

    def _failed(
        self,
        request_id: str,
        code: str,
        message: str,
    ) -> AgentPosixBridgeError:
        error = {"code": code, "message": message, "retryable": False}
        self.requests[request_id] = {
            "requestId": request_id,
            "method": "fs.apply",
            "state": "failed",
            "result": None,
            "error": error,
        }
        return self._error(code, message)

    def invoke(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        normalized = dict(params or {})
        if operation == "fs.request.status":
            request_id = str(normalized["requestId"])
            if request_id in self.closed_requests or request_id not in self.requests:
                raise self._error("RequestNotFound", "request is closed")
            return dict(self.requests[request_id])
        if operation == "fs.request.close":
            self.close_calls += 1
            request_id = str(normalized["requestId"])
            if self.close_failure_once:
                self.close_failure_once = False
                raise self._error("FsRequestCloseFailed", "request close failed")
            if self.close_completion_unknown_before_once:
                self.close_completion_unknown_before_once = False
                raise AgentPosixBridgeError(
                    "request close response was lost before completion",
                    error={"delivery": "completion_unknown"},
                )
            self.requests.pop(request_id, None)
            self.closed_requests.add(request_id)
            if self.close_completion_unknown_after_once:
                self.close_completion_unknown_after_once = False
                raise AgentPosixBridgeError(
                    "request close response was lost after completion",
                    error={"delivery": "completion_unknown"},
                )
            return {}
        if operation == "fs.snapshot.remove":
            request_id = str(normalized["requestId"])
            snapshot_id = str(normalized["snapshotId"])
            self.removed_snapshots.append(snapshot_id)
            result: dict[str, Any] = {}
            self.requests[request_id] = {
                "requestId": request_id,
                "method": operation,
                "state": "succeeded",
                "result": result,
                "error": None,
            }
            return result
        if operation != "fs.apply":
            raise AssertionError(operation)

        self.apply_calls += 1
        request_id = str(normalized["requestId"])
        existing = self.requests.get(request_id)
        if existing is not None:
            if existing["state"] == "succeeded":
                return dict(existing["result"])
            error = existing.get("error") or {}
            raise self._error(str(error.get("code")), str(error.get("message")))
        if self.needs_recovery:
            self.requests[request_id] = {
                "requestId": request_id,
                "method": "fs.apply",
                "state": "needs_recovery",
                "result": None,
                "error": {"code": "FsApplyNeedsRecovery"},
            }
            raise self._error("FsApplyNeedsRecovery", "apply needs recovery")
        if self.policy_rejected:
            raise self._failed(
                request_id,
                "FsApplyPolicyRejected",
                "safe content policy rejected the publication",
            )

        changed = {
            path
            for path in set(self.base) | set(self.target)
            if self.base.get(path) != self.target.get(path)
        }
        dependencies = {
            str(item["path"])
            for item in normalized.get("dependencies", [])
            if item.get("scope") == "content"
        }
        conflict = next(
            (
                path
                for path in sorted(changed | dependencies)
                if self.root.get(path) != self.base.get(path)
            ),
            None,
        )
        if conflict is not None:
            raise self._failed(
                request_id,
                "FsApplyConflict",
                f"optimistic dependency changed: {conflict}",
            )

        for path in changed:
            if path in self.target:
                self.root[path] = self.target[path]
            else:
                self.root.pop(path, None)
        result = {
            "status": "applied",
            "baseSnapshotId": normalized["baseSnapshotId"],
            "targetSnapshotId": normalized["targetSnapshotId"],
            "changedPaths": len(changed),
            "changedBytes": sum(len(self.target.get(path, b"")) for path in changed),
        }
        self.requests[request_id] = {
            "requestId": request_id,
            "method": "fs.apply",
            "state": "succeeded",
            "result": result,
            "error": None,
        }
        if self.lose_response:
            self.lose_response = False
            raise AgentPosixBridgeError(
                "apply response was lost",
                error={"delivery": "completion_unknown"},
            )
        return result


def _prepared_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: StatefulApplyClient,
    *,
    dependencies: list[dict[str, str]] | None = None,
) -> tuple[FileSearchRuntime, str]:
    monkeypatch.setattr(
        FileDeviceLeases,
        "release_device_pool_leases",
        lambda _self, _run: None,
    )
    runtime = FileSearchRuntime(tmp_path / ".gp")
    run_id = "run-thinkthread-publication"
    base = ArtifactRef(kind="fs_snapshot", id="fsnap-S0", provider="thinkthread")
    selected = ArtifactRef(
        kind="fs_snapshot", id="fsnap-selected", provider="thinkthread"
    )
    publication = ArtifactRef(kind="fs_snapshot", id="fsnap-P", provider="thinkthread")
    promotion = PromotionRecord(
        promotion_id="promotion-c001",
        run_id=run_id,
        candidate_id="c001",
        provider="thinkthread",
        strategy="fs_apply",
        base_artifact_ref=base,
        selected_artifact_ref=selected,
        publication_artifact_ref=publication,
        changed_files=["program.py"],
        provider_handle={"dependencies": dependencies or []},
        created_at=utc_timestamp(),
    )
    outcome = PublicationOutcome(
        run_id=run_id,
        revision=1,
        state="prepared",
        base_artifact_ref=base,
        selected_artifact_ref=selected,
        publication_artifact_ref=publication,
        changed_paths=["program.py"],
        dependency_summary={"dependencies": dependencies or []},
        updated_at=utc_timestamp(),
    )
    run = RunRecord(
        run_id=run_id,
        state=RunState.PROMOTED,
        frozen_spec_id="spec-test",
        source_path="project",
        source_artifact_ref=base,
        promotion=promotion,
        search_result="passed",
        publication_outcome=outcome,
        created_at=utc_timestamp(),
    )
    runtime.search_run.persist(run)
    from goal_plus.application.publication_contracts import publication_apply_intent
    from goal_plus.infrastructure.artifacts.thinkthread_publication import apply_contract

    identity, params = apply_contract(publication_apply_intent(promotion, 1))
    mutation = runtime.provider_mutations.prepare_provider_mutation(
        run_id=run_id,
        operation="apply",
        identity=identity,
        params=params,
    )
    run = runtime.search_run.state.read_run(run_id)
    assert run.promotion is not None
    assert run.publication_outcome is not None
    run.promotion.apply_request_id = mutation.request_id
    run.promotion.provider_handle["apply_request_id"] = mutation.request_id
    run.publication_outcome.apply_request_id = mutation.request_id
    runtime.search_run.persist(run)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    monkeypatch.setattr(
        runtime.publication_apply.state,
        "read_candidate",
        lambda _run_id, _candidate_id: SimpleNamespace(
            candidate_id="c001",
            iterations=[],
        ),
    )
    return runtime, run_id


def _record_owned_snapshot(
    runtime: FileSearchRuntime,
    run_id: str,
    operation: str,
    snapshot_id: str,
) -> None:
    identity: dict[str, Any] = {"test_snapshot_id": snapshot_id}
    params = {"snapshotId": snapshot_id}
    if operation in {"baseline_snapshot", "publication_snapshot"}:
        identity["root_thinkthread_id"] = "tt-root"
    if operation == "branch_snapshot":
        identity["branch_id"] = "fsbranch-owned"
    mutation = runtime.provider_mutations.prepare_provider_mutation(
        run_id=run_id,
        operation=operation,
        identity=identity,
        params=params,
    )
    result = (
        {
            "snapshot": {
                "snapshotId": snapshot_id,
                "ownerThinkthreadId": "tt-root",
            }
        }
        if operation == "publication_snapshot"
        else {
            "snapshotId": snapshot_id,
            **(
                {"createdFromBranchId": "fsbranch-owned"}
                if operation == "branch_snapshot"
                else {"ownerThinkthreadId": "tt-root"}
            ),
        }
    )
    runtime.provider_mutations.update_provider_mutation(
        run_id=run_id,
        request_id=mutation.request_id,
        state="closed",
        result=result,
    )


def test_optimistic_apply_preserves_unrelated_root_change_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    client.root["notes.txt"] = b"main changed this\n"
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)

    first = runtime.apply_promotion(run_id)
    second = runtime.apply_promotion(run_id)

    assert first.state == second.state == "applied"
    assert client.root["project/program.py"] == b"candidate\n"
    assert client.root["notes.txt"] == b"main changed this\n"
    assert client.apply_calls == 1
    assert not any(
        item.operation == "apply" and item.state != "closed"
        for item in runtime.search_run.state.read_run(run_id).provider_mutations
    )
    notifications = runtime.main_agent_notifications(run_id)
    assert len(notifications) == 1
    assert notifications[0].kind == "publication_applied"
    assert notifications[0].payload["publication_state"] == "applied"

    delivered = runtime.acknowledge_main_agent_notification(
        run_id,
        notifications[0].notification_id,
    )
    replayed = runtime.acknowledge_main_agent_notification(
        run_id,
        notifications[0].notification_id,
    )

    assert delivered == replayed
    assert delivered.state == "delivered"
    assert runtime.main_agent_notifications(run_id) == []
    outcome = runtime.search_run.state.read_run(run_id).publication_outcome
    assert outcome is not None
    assert outcome.notification_state == "delivered"


@pytest.mark.parametrize(
    ("changed_path", "dependencies"),
    [
        ("project/program.py", []),
        (
            "project/config.json",
            [{"path": "project/config.json", "scope": "content"}],
        ),
    ],
)
def test_optimistic_apply_conflict_is_atomic_and_creates_pending_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_path: str,
    dependencies: list[dict[str, str]],
) -> None:
    client = StatefulApplyClient()
    client.root[changed_path] = b"main concurrent edit\n"
    before = dict(client.root)
    runtime, run_id = _prepared_runtime(
        tmp_path,
        monkeypatch,
        client,
        dependencies=dependencies,
    )

    promotion = runtime.apply_promotion(run_id)

    assert promotion.state == "awaiting_main_integration"
    assert client.root == before
    run = runtime.search_run.state.read_run(run_id)
    assert run.publication_outcome is not None
    assert run.publication_outcome.apply_outcome == "conflict"
    assert run.publication_outcome.pending_artifact_path is not None
    assert run.publication_outcome.pending_artifact_path.is_file()
    payload = run.publication_outcome.pending_artifact_path.read_text(encoding="utf-8")
    assert hashlib.sha256(payload.encode()).hexdigest()
    assert "fsnap-S0" in payload
    assert "fsnap-selected" in payload
    assert "fsnap-P" in payload
    assert not (runtime.search_run.state.run_dir(run_id) / "artifacts" / "candidate.patch").exists()
    notifications = runtime.main_agent_notifications(run_id)
    assert len(notifications) == 1
    assert notifications[0].kind == "publication_pending_integration"
    assert notifications[0].payload["pending_artifact_path"] == str(
        run.publication_outcome.pending_artifact_path
    )


def test_policy_rejection_is_confirmed_pending_integration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    client.policy_rejected = True
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)

    promotion = runtime.apply_promotion(run_id)

    assert promotion.state == "awaiting_main_integration"
    run = runtime.search_run.state.read_run(run_id)
    assert run.publication_outcome is not None
    assert run.publication_outcome.apply_outcome == "policy_rejected"
    assert run.publication_outcome.provider_error["code"] == "FsApplyPolicyRejected"
    mutation = next(
        item for item in run.provider_mutations if item.operation == "apply"
    )
    assert mutation.state == "failed"
    assert mutation.close_state == "closed"
    assert client.close_calls == 1


def test_apply_response_loss_recovers_same_request_without_duplicate_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    client.lose_response = True
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)
    expected = runtime.search_run.state.read_run(run_id).promotion
    assert expected is not None

    promotion = runtime.apply_promotion(run_id)

    assert promotion.state == "applied"
    assert client.apply_calls == 1
    assert promotion.apply_request_id == expected.apply_request_id
    assert client.root["project/program.py"] == b"candidate\n"


@pytest.mark.parametrize("mismatch", [None, "request", "artifact"])
def test_apply_uses_narrow_capabilities_and_checks_success_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str | None,
) -> None:
    from goal_plus.ports.publication import PublicationApplyObservation

    client = StatefulApplyClient()
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)
    application = runtime.publication_apply
    state = application.state
    describe = application.thinkthread.describe_apply

    def apply(intent):
        run = state.read_run(run_id)
        assert run.promotion.state == "applying"
        return PublicationApplyObservation(
            "applied",
            "req-other" if mismatch == "request" else describe(intent).request_id,
            intent.base_artifact_ref,
            ArtifactRef(kind="fs_snapshot", provider="thinkthread", id="fsnap-other")
            if mismatch == "artifact" else intent.target_artifact_ref,
        )

    application.state = SimpleNamespace(
        read_run=state.read_run, read_candidate=state.read_candidate,
        lock_run=state.lock_run, write_pending=state.write_pending,
    )
    application.io = SimpleNamespace()
    application.thinkthread = SimpleNamespace(describe_apply=describe, apply=apply)

    result = runtime.apply_promotion(run_id)

    assert result.state == ("recovering" if mismatch else "applied")
    assert client.apply_calls == 0
    if mismatch:
        assert result.apply_result == {}
        assert state.read_run(run_id).publication_outcome.recovery is not None


def test_request_close_completion_unknown_after_close_is_confirmed_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    client.close_completion_unknown_after_once = True
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)

    promotion = runtime.apply_promotion(run_id)

    assert promotion.state == "applied"
    mutation = next(
        item
        for item in runtime.search_run.state.read_run(run_id).provider_mutations
        if item.operation == "apply"
    )
    assert mutation.state == "closed"
    assert mutation.close_state == "closed"
    assert mutation.close_attempts == 1
    assert client.close_calls == 1
    assert client.apply_calls == 1


@pytest.mark.parametrize(
    "fault",
    ["completion_unknown_before", "definitive_failure"],
)
def test_request_close_recovery_retries_only_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    client = StatefulApplyClient()
    if fault == "completion_unknown_before":
        client.close_completion_unknown_before_once = True
    else:
        client.close_failure_once = True
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)

    promotion = runtime.apply_promotion(run_id)

    assert promotion.state == "applied"
    mutation = next(
        item
        for item in runtime.search_run.state.read_run(run_id).provider_mutations
        if item.operation == "apply"
    )
    assert mutation.state == "succeeded"
    assert mutation.close_state == "needs_recovery"
    assert mutation.close_attempts == 1
    assert mutation.close_error
    assert client.apply_calls == 1

    reconciled = runtime.reconcile_thinkthread_request_closes(
        run_id,
        client=client,  # type: ignore[arg-type]
    )

    assert reconciled == {"closed": [mutation.request_id], "needs_recovery": []}
    recovered = next(
        item
        for item in runtime.search_run.state.read_run(run_id).provider_mutations
        if item.operation == "apply"
    )
    assert recovered.state == "closed"
    assert recovered.close_state == "closed"
    assert recovered.close_attempts == 2
    assert recovered.close_error == {}
    assert client.close_calls == 2
    assert client.apply_calls == 1


def test_needs_recovery_keeps_apply_fenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    client.needs_recovery = True
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)

    promotion = runtime.apply_promotion(run_id)

    assert promotion.state == "recovering"
    run = runtime.search_run.state.read_run(run_id)
    assert run.publication_outcome is not None
    assert run.publication_outcome.state == "recovering"
    assert run.publication_outcome.apply_outcome == "needs_recovery"
    assert run.publication_outcome.notification_state == "not_applicable"
    assert run.publication_outcome.pending_artifact_path is None
    assert run.publication_outcome.recovery is not None
    assert run.publication_outcome.recovery.request_id == promotion.apply_request_id
    assert client.root == client.base

    replayed = FileSearchRuntime(runtime.root_dir).apply_promotion(run_id)
    assert replayed.state == "recovering"
    assert client.apply_calls == 1
    recovered_run = runtime.search_run.state.read_run(run_id)
    assert recovered_run.publication_outcome is not None
    assert recovered_run.publication_outcome.recovery is not None
    assert recovered_run.publication_outcome.recovery.attempts == 2


def test_restart_reconciles_recovering_apply_before_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    client.needs_recovery = True
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)
    first = runtime.apply_promotion(run_id)
    assert first.state == "recovering"
    assert first.apply_request_id is not None
    result = {
        "status": "applied",
        "baseSnapshotId": "fsnap-S0",
        "targetSnapshotId": "fsnap-P",
        "changedPaths": 1,
        "changedBytes": len(b"candidate\n"),
    }
    client.requests[first.apply_request_id] = {
        "requestId": first.apply_request_id,
        "method": "fs.apply",
        "state": "succeeded",
        "result": result,
        "error": None,
    }

    recovered = FileSearchRuntime(runtime.root_dir).apply_promotion(run_id)

    assert recovered.state == "applied"
    assert recovered.apply_request_id == first.apply_request_id
    assert client.apply_calls == 1
    assert not (
        runtime.search_run.state.run_dir(run_id) / "artifacts" / "pending-integration.json"
    ).exists()
    notifications = runtime.main_agent_notifications(run_id)
    assert len(notifications) == 1
    assert notifications[0].kind == "publication_applied"


def test_pending_integration_pins_all_authoritative_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    client.root["project/program.py"] = b"main concurrent edit\n"
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)
    for operation, snapshot_id in (
        ("baseline_snapshot", "fsnap-S0"),
        ("branch_snapshot", "fsnap-selected"),
        ("publication_snapshot", "fsnap-P"),
    ):
        _record_owned_snapshot(runtime, run_id, operation, snapshot_id)
    runtime.apply_promotion(run_id)
    run_dir = runtime.search_run.state.run_dir(run_id)
    (run_dir / "report.md").write_text("report\n", encoding="utf-8")
    (run_dir / "report.html").write_text("report\n", encoding="utf-8")

    cleanup = FileSearchRuntime(runtime.root_dir).cleanup_thinkthread_snapshots(run_id)

    assert cleanup["state"] == "retained"
    assert client.removed_snapshots == []
    records = runtime.search_run.state.read_run(run_id).provider_cleanup
    assert {item.resource_id for item in records} == {
        "fsnap-S0",
        "fsnap-selected",
        "fsnap-P",
    }
    assert all(
        "publication:awaiting_main_integration" in item.pins for item in records
    )


def test_cross_run_reference_pins_only_while_run_or_recovery_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)
    _record_owned_snapshot(runtime, run_id, "baseline_snapshot", "fsnap-S0")
    runtime.apply_promotion(run_id)
    run_dir = runtime.search_run.state.run_dir(run_id)
    (run_dir / "report.md").write_text("report\n", encoding="utf-8")
    (run_dir / "report.html").write_text("report\n", encoding="utf-8")
    other = RunRecord(
        run_id="run-other",
        state=RunState.RUNNING,
        frozen_spec_id="spec-other",
        source_path="project",
        source_artifact_ref=ArtifactRef(
            kind="fs_snapshot",
            id="fsnap-S0",
            provider="thinkthread",
        ),
        created_at=utc_timestamp(),
    )
    runtime.search_run.persist(other)

    retained = runtime.cleanup_thinkthread_snapshots(run_id)
    assert retained["state"] == "retained"
    assert retained["retained"][0]["pins"] == ["run:run-other"]
    assert client.removed_snapshots == []

    other.state = RunState.PROMOTED
    runtime.search_run.persist(other)
    recovery = runtime.provider_mutations.prepare_provider_mutation(
        run_id=other.run_id,
        operation="verifier_run",
        identity={"snapshot_id": "fsnap-S0"},
        params={"snapshotId": "fsnap-S0"},
    )
    runtime.provider_mutations.update_provider_mutation(
        run_id=other.run_id,
        request_id=recovery.request_id,
        state="needs_recovery",
        error={"code": "CompletionUnknown"},
    )
    recovery_retained = runtime.cleanup_thinkthread_snapshots(run_id)
    assert recovery_retained["state"] == "retained"
    assert recovery_retained["retained"][0]["pins"] == ["run:run-other"]

    runtime.provider_mutations.update_provider_mutation(
        run_id=other.run_id,
        request_id=recovery.request_id,
        state="closed",
        result={"exit": {"code": 0}},
        error={},
    )
    removed = FileSearchRuntime(runtime.root_dir).cleanup_thinkthread_snapshots(run_id)

    assert removed["state"] == "complete"
    assert removed["removed"] == ["fsnap-S0"]
    assert client.removed_snapshots == ["fsnap-S0"]
    record = runtime.search_run.state.read_run(run_id).provider_cleanup[0]
    assert record.state == "removed"
    assert record.pins == []


def test_provider_recovery_anchor_pins_owned_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)
    _record_owned_snapshot(runtime, run_id, "baseline_snapshot", "fsnap-S0")
    runtime.apply_promotion(run_id)
    run_dir = runtime.search_run.state.run_dir(run_id)
    (run_dir / "report.md").write_text("report\n", encoding="utf-8")
    (run_dir / "report.html").write_text("report\n", encoding="utf-8")
    recovery = runtime.provider_mutations.prepare_provider_mutation(
        run_id=run_id,
        operation="verifier_run",
        identity={"snapshot_id": "fsnap-S0"},
        params={"snapshotId": "fsnap-S0"},
    )
    runtime.provider_mutations.update_provider_mutation(
        run_id=run_id,
        request_id=recovery.request_id,
        state="needs_recovery",
        error={"code": "CompletionUnknown"},
    )

    retained = runtime.cleanup_thinkthread_snapshots(run_id)

    assert retained["state"] == "retained"
    assert retained["retained"][0]["pins"] == [
        f"provider_recovery:verifier_run:{recovery.request_id}"
    ]
    assert client.removed_snapshots == []


def test_removed_snapshot_artifact_view_fails_before_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)
    artifact = ArtifactRef(
        kind="fs_snapshot",
        id="fsnap-evidence",
        provider="thinkthread",
    )
    runtime.artifact_lifecycle.record_cleanup(
        run_id,
        artifact.id,
        purposes={"candidate:c001:branch_snapshot"},
        ownership_request_ids={"req-create"},
        state="removed",
        request_id="req-remove",
    )
    iteration = IterationRecord(
        iteration=1,
        agent_session_id="agent-1",
        artifact_ref=artifact,
        created_at=utc_timestamp(),
    )
    monkeypatch.setattr(
        runtime.annotation_context.source,
        "read_context_candidate",
        lambda _run_id, _candidate_id: SimpleNamespace(
            workspace=tmp_path / "unused",
            workspace_provider="thinkthread",
            iterations=[iteration],
        ),
    )

    def unexpected_provider_access(_backend: str) -> None:
        raise AssertionError("provider must not be accessed after cleanup")

    monkeypatch.setattr(
        "goal_plus.infrastructure.artifacts.reader.thinkthread_artifact_view",
        unexpected_provider_access,
    )
    task = EvidenceAnnotationTask(
        task_id="annotation_settlement-test",
        settlement_id="settlement-test",
        run_id=run_id,
        candidate_id="c001",
        iteration=1,
        attempt_base_artifact_ref=ArtifactRef(
            kind="fs_snapshot",
            id="fsnap-base",
            provider="thinkthread",
        ),
        attempt_artifact_ref=artifact,
        artifact_views=[
            EvidenceArtifactViewReference(
                view_id="view-target",
                role="target",
                candidate_id="c001",
                iteration=1,
                artifact_ref=artifact,
                workspace_provider="thinkthread",
                display_name="target",
            )
        ],
        created_at=utc_timestamp(),
        updated_at=utc_timestamp(),
    )

    with pytest.raises(RuntimeError, match="removed after terminal cleanup"):
        runtime.annotation_context.open_views(task)


def test_cleanup_never_claims_snapshot_with_mismatched_provider_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)
    mutation = runtime.provider_mutations.prepare_provider_mutation(
        run_id=run_id,
        operation="baseline_snapshot",
        identity={"root_thinkthread_id": "tt-root"},
        params={},
    )
    runtime.provider_mutations.update_provider_mutation(
        run_id=run_id,
        request_id=mutation.request_id,
        state="closed",
        result={
            "snapshotId": "fsnap-not-owned",
            "ownerThinkthreadId": "tt-other-root",
        },
    )

    cleanup = runtime.cleanup_thinkthread_snapshots(run_id)

    assert cleanup == {"state": "not_applicable", "removed": [], "retained": []}
    assert client.removed_snapshots == []
    assert runtime.search_run.state.read_run(run_id).provider_cleanup == []


def test_apply_intent_hash_covers_dependencies_and_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    dependencies = [{"path": "project/config.json", "scope": "content"}]
    runtime, run_id = _prepared_runtime(
        tmp_path,
        monkeypatch,
        client,
        dependencies=dependencies,
    )
    run = runtime.search_run.state.read_run(run_id)
    mutation = next(
        item for item in run.provider_mutations if item.operation == "apply"
    )
    assert run.promotion is not None
    from goal_plus.application.publication_contracts import publication_apply_intent
    from goal_plus.infrastructure.artifacts.thinkthread_publication import apply_contract

    _, params = apply_contract(publication_apply_intent(run.promotion, 1))
    exact = {**params, "requestId": mutation.request_id}

    assert (
        mutation.params_sha256
        == hashlib.sha256(canonical_json(exact).encode()).hexdigest()
    )
    assert mutation.identity["dependencies"] == dependencies
    assert mutation.identity["policy_id"] == "safe_content_v1"


def test_terminal_publication_recreates_notification_after_crash_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulApplyClient()
    runtime, run_id = _prepared_runtime(tmp_path, monkeypatch, client)
    runtime.apply_promotion(run_id)
    run = runtime.search_run.state.read_run(run_id)
    assert run.publication_outcome is not None
    notification_path = runtime.notifications.state.notification_path(
        run_id,
        run.publication_outcome.revision,
    )
    notification_path.unlink()
    run.publication_outcome.notification_state = "not_applicable"
    runtime.search_run.persist(run)

    recovered = FileSearchRuntime(runtime.root_dir).main_agent_notifications(run_id)

    assert len(recovered) == 1
    assert recovered[0].state == "pending"
    assert notification_path.is_file()
    persisted = runtime.search_run.state.read_run(run_id)
    assert persisted.publication_outcome is not None
    assert persisted.publication_outcome.notification_state == "pending"
