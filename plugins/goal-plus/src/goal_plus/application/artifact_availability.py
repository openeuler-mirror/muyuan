"""Read-only cleanup projection; it never recreates a removed artifact."""

from typing import Literal

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.ports.state import ProviderResourceCleanupRecord

ArtifactAvailability = Literal["available", "removal_pending", "removed", "unknown"]


def artifact_availability(
    artifact_ref: ArtifactRef | None,
    provider_cleanup: list[ProviderResourceCleanupRecord],
) -> ArtifactAvailability:
    """Project provider cleanup state without discarding artifact identity."""

    if artifact_ref is None:
        return "unknown"
    if artifact_ref.kind != "fs_snapshot" or artifact_ref.provider != "thinkthread":
        return "available"
    cleanup = next(
        (
            item
            for item in reversed(provider_cleanup)
            if item.provider == "thinkthread"
            and item.resource_kind == "fs_snapshot"
            and item.resource_id == artifact_ref.id
        ),
        None,
    )
    if cleanup is None or cleanup.state == "retained":
        return "available"
    if cleanup.state in {"prepared", "removing", "needs_recovery"}:
        return "removal_pending"
    if cleanup.state in {"removed", "already_absent"}:
        return "removed"
    return "unknown"
