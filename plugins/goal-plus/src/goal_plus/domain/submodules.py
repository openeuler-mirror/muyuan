"""Immutable facts for a Git workspace containing formal submodules."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from goal_plus.domain.base import SearchModel


class SubmoduleNode(SearchModel):
    path: str
    url: str
    repository_identity: str
    gitlink: str
    child_head: str
    depth: int = Field(ge=1)


class SubmoduleTopology(SearchModel):
    parent_head: str
    nodes: list[SubmoduleNode] = Field(default_factory=list)
    topology_digest: str


class SubmoduleCheckError(RuntimeError):
    _REASONS: ClassVar[dict[str, str]] = {
        "SubmoduleGitUnavailable": "the Git inspection command failed",
        "SubmoduleTopologyInvalid": "committed .gitmodules and gitlink topology is inconsistent",
        "SubmoduleTopologyCycle": "recursive submodule traversal reached the same repository twice",
        "SubmoduleWorkspaceDirty": "the repository has uncommitted changes or an in-progress operation",
        "SubmoduleUnregisteredGit": "the worktree contains a nested Git root that is not a committed submodule",
        "SubmoduleRepositoryMismatch": "the checkout resolves to a different Git root than expected",
        "SubmoduleUninitialized": "the committed child is not initialized in the worktree",
        "SubmoduleHeadMismatch": "the child HEAD differs from the parent gitlink",
        "SubmoduleObjectUnavailable": "the required commit object is missing or the repository is shallow",
    }

    def __init__(
        self,
        failure_class: str,
        *,
        path: str,
        stage: str,
        expected: str | None = None,
        actual: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.failure_class = failure_class
        self.path = path
        self.stage = stage
        self.expected = expected
        self.actual = actual
        self.reason = reason or self._REASONS.get(failure_class, "the submodule workspace check failed")
        super().__init__(
            f"{failure_class}: {path or '.'} at {stage}; reason: {self.reason}"
            + (f" (expected {expected}, actual {actual})" if expected is not None or actual is not None else "")
        )
