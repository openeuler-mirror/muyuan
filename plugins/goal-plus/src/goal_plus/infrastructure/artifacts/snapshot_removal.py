"""Exact provider mutation requests for snapshot cleanup."""

from goal_plus.ports.artifact_lifecycle import SnapshotRemovalResult
from typing import Callable
from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient
from goal_plus.infrastructure.artifacts.thinkthread_mutations import ThinkThreadMutations
from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError
from goal_plus.infrastructure.artifacts.thinkthread_mutations import provider_error

class ThinkThreadSnapshotRemoval:
    def __init__(self, client: Callable[[], AgentPosixSdkClient], mutations: ThinkThreadMutations):
        self.client, self.mutations = client, mutations

    def reconcile_closes(self, run_id):
        self.mutations.reconcile_thinkthread_request_closes(run_id)

    def prepare_removal(self, *, run_id, snapshot_id):
        return self.mutations.prepare_provider_mutation(
            run_id=run_id,
            operation="snapshot_remove",
            identity={"snapshot_id": snapshot_id},
            params={"snapshotId": snapshot_id},
        )

    def remove(self, *, run_id, snapshot_id):
        request_id = self.mutations.thinkthread_request_id(
            "snapshot_remove", run_id, {"snapshot_id": snapshot_id},
        )
        try:
            client = self.client()
            client.preflight()
            result, executed_request_id = self.mutations.invoke_provider_mutation(
                client=client,
                run_id=run_id,
                method="fs.snapshot.remove",
                operation="snapshot_remove",
                identity={"snapshot_id": snapshot_id},
                params={"snapshotId": snapshot_id},
                timeout_seconds=120.0,
            )
            return SnapshotRemovalResult("removed", executed_request_id, snapshot_id, result)
        except AgentPosixBridgeError as exc:
            if exc.code == "FsSnapshotNotFound":
                details = {"alreadyAbsent": True}
                self.mutations.update_provider_mutation(
                    run_id=run_id, request_id=request_id, state="closed",
                    result=details, error={},
                )
                return SnapshotRemovalResult("already_absent", request_id, snapshot_id, details)
            return SnapshotRemovalResult(
                "needs_recovery" if exc.completion_unknown else "failed",
                request_id, snapshot_id, error=provider_error(exc),
            )
        except Exception as exc:
            return SnapshotRemovalResult(
                "needs_recovery" if "NeedsRecovery" in str(exc) else "failed",
                request_id, snapshot_id, error=provider_error(exc),
            )
