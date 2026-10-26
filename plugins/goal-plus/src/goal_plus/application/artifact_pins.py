"""Snapshot ownership and retention policy over persisted provider identities."""

from typing import Any
from goal_plus.domain.search import RunState
from goal_plus.ports.state import ProviderMutationRecord

SNAPSHOT_CLEANUP_TERMINAL_RUN_STATES = frozenset(
    {
        RunState.PROMOTED,
        RunState.FAILED,
        RunState.ABORTED,
        RunState.SELECTION_BLOCKED,
    }
)


def snapshot_id_from_mutation(
    mutation: ProviderMutationRecord,
) -> str | None:
    if mutation.state not in {"succeeded", "closed"}:
        return None
    value: object
    if mutation.operation == "baseline_snapshot":
        if mutation.result.get("ownerThinkthreadId") != mutation.identity.get(
            "root_thinkthread_id"
        ):
            return None
        value = mutation.result.get("snapshotId")
    elif mutation.operation == "branch_snapshot":
        if mutation.result.get("createdFromBranchId") != mutation.identity.get(
            "branch_id"
        ):
            return None
        value = mutation.result.get("snapshotId")
    elif mutation.operation == "inner_git_settlement":
        value = mutation.result.get("targetSnapshotId")
    elif mutation.operation == "publication_snapshot":
        snapshot = mutation.result.get("snapshot")
        if not isinstance(snapshot, dict) or snapshot.get(
            "ownerThinkthreadId"
        ) != mutation.identity.get("root_thinkthread_id"):
            return None
        value = snapshot.get("snapshotId") if isinstance(snapshot, dict) else None
    else:
        return None
    return value if isinstance(value, str) and value.startswith("fsnap-") else None


def snapshot_refs_in_payload(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        if (
            value.get("kind") == "fs_snapshot"
            and value.get("provider") == "thinkthread"
            and isinstance(value.get("id"), str)
        ):
            found.add(str(value["id"]))
        for nested in value.values():
            found.update(snapshot_refs_in_payload(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(snapshot_refs_in_payload(nested))
    return found


def snapshot_recovery_refs_in_payload(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        mutations = value.get("provider_mutations")
        if isinstance(mutations, list):
            for mutation in mutations:
                if not isinstance(mutation, dict) or mutation.get("state") not in {
                    "platform_mutation_started",
                    "needs_recovery",
                }:
                    continue
                identity = mutation.get("identity")
                if not isinstance(identity, dict):
                    continue
                found.update(snapshot_refs_in_provider_identity(identity))
    return found


def snapshot_refs_in_provider_identity(
    identity: dict[str, Any],
) -> set[str]:
    return {
        item
        for key, item in identity.items()
        if (key == "snapshot_id" or key.endswith("_snapshot_id"))
        and isinstance(item, str)
        and item.startswith("fsnap-")
    }


def run_keeps_external_snapshot_refs(payload: dict[str, Any]) -> bool:
    state = payload.get("state")
    if state not in SNAPSHOT_CLEANUP_TERMINAL_RUN_STATES:
        return True
    outcome = payload.get("publication_outcome")
    return isinstance(outcome, dict) and outcome.get("state") in {
        "prepared",
        "applying",
        "recovering",
        "awaiting_main_integration",
    }
