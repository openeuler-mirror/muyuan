"""Pure contracts and structural invariants."""

from __future__ import annotations

from pydantic import (
    Field,
    model_validator,
)

from goal_plus.domain.artifacts import (
    ArtifactRef,
    ExecutionScope,
)
from goal_plus.domain.base import SearchModel


class WorkerExecutionResult(SearchModel):
    """Provider-neutral result of one completed host worker execution."""

    output: str = ""
    execution_scope: ExecutionScope
    artifact_ref: ArtifactRef | None = None
    thinkthread_id: str | None = Field(default=None, min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def private_execution_requires_snapshot_identity(self) -> "WorkerExecutionResult":
        if self.execution_scope == "native_root":
            if self.thinkthread_id is not None:
                raise ValueError(
                    "native_root execution cannot carry a ThinkThread identity"
                )
            return self
        if self.thinkthread_id is None:
            raise ValueError(
                "thinkthread_private execution requires a ThinkThread identity"
            )
        if (
            self.artifact_ref is None
            or self.artifact_ref.kind != "fs_snapshot"
            or self.artifact_ref.provider != "thinkthread"
        ):
            raise ValueError(
                "thinkthread_private execution requires a ThinkThread fs_snapshot"
            )
        return self
