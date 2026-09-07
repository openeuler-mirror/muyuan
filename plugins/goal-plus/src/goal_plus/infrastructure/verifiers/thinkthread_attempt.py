"""ThinkThread snapshot, deterministic verifier and inner-Git provider I/O."""

from pathlib import Path
from typing import Any, Literal
import json
import sys
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import IterationDisposition, ScoreReport
from goal_plus.domain.search import FrozenSpec, VerifierCommand
from goal_plus.domain.results_ledger import format_result_score, valid_inner_git_head
from goal_plus.ports.state import RunRecord
from goal_plus.ports.thinkthread import ThinkThreadClient
from goal_plus.ports.candidate_settlement import InnerGitSettlement
from goal_plus.ports.thinkthread_verification import (
    AttemptChildObservation,
    AttemptSnapshot,
    InnerGitSettlementResult,
)
from goal_plus.thinkthread_artifacts import (
    ThinkThreadSnapshotReader,
    decode_fs_run_output,
)
from goal_plus.infrastructure.verifiers.thinkthread import ThinkThreadVerifiers
from goal_plus.infrastructure.artifacts.thinkthread_mutations import (
    ThinkThreadMutations,
)


class ProviderThinkThreadAttempt:
    def __init__(
        self,
        client: ThinkThreadClient,
        verifiers: ThinkThreadVerifiers,
        mutations: ThinkThreadMutations,
    ) -> None:
        self.client, self.thinkthread_verifiers, self.provider_mutations = (
            client,
            verifiers,
            mutations,
        )
        self.reader = ThinkThreadSnapshotReader(client)

    def read_child(self, thinkthread_id: str) -> AttemptChildObservation:
        child = self.client.invoke("thinkthread.get", {"id": thinkthread_id})
        attachment = child.get("fs")
        if (
            not isinstance(attachment, dict)
            or attachment.get("kind") != "private"
            or not isinstance(attachment.get("fsBranchId"), str)
            or not isinstance(child.get("thinkthreadId"), str)
            or not isinstance(child.get("parentThinkthreadId"), str)
            or not isinstance(child.get("agentState"), str)
            or not isinstance(child.get("pendingWake", False), bool)
        ):
            raise RuntimeError("ThinkThread attempt Child observation is invalid")
        return AttemptChildObservation(
            thinkthread_id=child["thinkthreadId"],
            parent_thinkthread_id=child["parentThinkthreadId"],
            branch_id=attachment["fsBranchId"],
            ready=child["agentState"] == "ready",
            pending_wake=child.get("pendingWake", False),
        )

    def changed_files(self, base: ArtifactRef, target: ArtifactRef) -> list[str]:
        return self.reader.changed_files(base, target)

    def canonical_digest(self, base: ArtifactRef, target: ArtifactRef) -> str:
        return self.reader.canonical_digest(base, target)

    def capture(
        self,
        *,
        run_id: str,
        candidate_id: str,
        branch_id: str,
        iteration: int,
        attempt_id: str,
        recovery_deadline: float | None,
    ) -> AttemptSnapshot:
        result, request_id = self.provider_mutations.invoke_provider_mutation(
            client=self.client,
            run_id=run_id,
            candidate_id=candidate_id,
            method="fs.branch.snapshot",
            operation="branch_snapshot",
            identity={
                "candidate_id": candidate_id,
                "branch_id": branch_id,
                "iteration": iteration,
                "attempt_id": attempt_id,
            },
            params={"branchId": branch_id},
            timeout_seconds=120.0,
            recovery_deadline=recovery_deadline,
        )
        snapshot_id = result.get("snapshotId")
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith("fsnap-"):
            raise RuntimeError("fs.branch.snapshot omitted an exact snapshot identity")
        created_from = result.get("createdFromBranchId")
        if not isinstance(created_from, str) or not created_from:
            raise RuntimeError("fs.branch.snapshot omitted its source branch")
        return AttemptSnapshot(
            artifact_ref=ArtifactRef(
                kind="fs_snapshot", id=snapshot_id, provider="thinkthread",
            ),
            branch_id=created_from,
            request_id=request_id,
        )

    def run_commands(
        self,
        *,
        run: RunRecord,
        frozen: FrozenSpec,
        candidate_id: str,
        artifact_ref: ArtifactRef,
        commands: list[VerifierCommand],
        phase: Literal["candidate"],
        iteration: int,
        recovery_deadline: float | None,
    ) -> ScoreReport:
        return self.thinkthread_verifiers.run_thinkthread_commands(
            client=self.client,
            run=run,
            frozen=frozen,
            candidate_id=candidate_id,
            artifact_ref=artifact_ref,
            commands=commands,
            phase=phase,
            iteration=iteration,
            recovery_deadline=recovery_deadline,
        )

    def settle_inner_git(
        self,
        *,
        run_id: str,
        candidate_id: str,
        attempt_id: str,
        attempt_artifact_ref: ArtifactRef,
        source_relative_path: str,
        iteration: int,
        metric_name: str,
        score: float | None,
        status: str,
        hypothesis: str,
        disposition: IterationDisposition,
        attempt_git_head: str,
        result_count_before: int,
        restore_git_head: str | None,
        recovery_deadline: float | None,
    ) -> InnerGitSettlementResult:
        argv = [
            sys.executable,
            "-I",
            str((Path(__file__).resolve().parents[2] / "inner_git.py").resolve()),
            "--workspace",
            ".",
            "settle-attempt",
            "--attempt-git-head",
            attempt_git_head,
            "--iteration",
            str(iteration),
            "--metric-name",
            metric_name,
            "--score",
            "null" if score is None else format_result_score(score),
            "--status",
            status,
            "--hypothesis",
            hypothesis,
        ]
        if restore_git_head is not None:
            argv.extend(("--restore-git-head", restore_git_head))
        params = {
            "snapshotId": attempt_artifact_ref.id,
            "writes": "snapshot",
            "invocation": {
                "argv": argv,
                "cwd": source_relative_path,
            },
            "limits": {
                "timeoutMs": 60_000,
                "maxOutputBytes": 64 * 1024,
            },
        }
        result, request_id = self.provider_mutations.invoke_provider_mutation(
            client=self.client,
            run_id=run_id,
            candidate_id=candidate_id,
            method="fs.run",
            operation="inner_git_settlement",
            identity={
                "candidate_id": candidate_id,
                "attempt_id": attempt_id,
                "attempt_snapshot_id": attempt_artifact_ref.id,
                "attempt_git_head": attempt_git_head,
                "iteration": iteration,
                "disposition": disposition,
                "restore_git_head": restore_git_head,
            },
            params=params,
            timeout_seconds=65.0,
            recovery_deadline=recovery_deadline,
        )
        stdout, stderr = decode_fs_run_output(result)
        returncode, failure_class = self.thinkthread_verifiers.thinkthread_exit_status(
            result
        )
        if returncode != 0:
            detail = (stderr or stdout).strip()[-2000:]
            raise RuntimeError(
                "ThinkThreadInnerGitSettlementFailed: "
                f"{failure_class or returncode}: {detail}"
            )
        payload: dict[str, Any] | None = None
        for line in reversed(stdout.splitlines()):
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                payload = decoded
                break
        target_snapshot_id = result.get("targetSnapshotId")
        if (
            payload is None
            or payload.get("ok") is not True
            or payload.get("protocol") != "goal-plus.inner-git.v1"
            or payload.get("adapter") != "autoresearch-git"
            or payload.get("attempt_git_head") != attempt_git_head
            or not valid_inner_git_head(payload.get("baseline_git_head"))
            or not valid_inner_git_head(payload.get("ledger_git_head"))
            or type(payload.get("result_count")) is not int
            or payload.get("result_count") != result_count_before + 1
            or payload.get("restored_to_git_head") != restore_git_head
            or not isinstance(target_snapshot_id, str)
            or not target_snapshot_id.startswith("fsnap-")
        ):
            raise RuntimeError(
                "ThinkThreadInnerGitSettlementInvalid: helper or provider result "
                "did not match the attempt"
            )
        return InnerGitSettlementResult(
            artifact_ref=ArtifactRef(
                kind="fs_snapshot",
                id=target_snapshot_id,
                provider="thinkthread",
            ),
            request_id=request_id,
            settlement=InnerGitSettlement(
                attempt_git_head=payload["attempt_git_head"],
                baseline_git_head=payload["baseline_git_head"],
                ledger_git_head=payload["ledger_git_head"],
                result_count=payload["result_count"],
                restored_to_git_head=payload.get("restored_to_git_head"),
            ),
        )
