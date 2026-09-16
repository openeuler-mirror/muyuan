"""Initial run storage, source and resource capabilities."""

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from goal_plus.domain.agents import AgentHarnessKind
from goal_plus.domain.artifacts import ArtifactRef, WorkspaceProvider
from goal_plus.domain.evidence import ScoreReport
from goal_plus.domain.search import (
    FrozenSpec,
    RunState,
    SearchPlan,
    SearchSpec,
)
from goal_plus.ports.state import RunRecord

DEVICE_LEASE_RELEASE_STATES = frozenset(
    {RunState.PROMOTED, RunState.ABORTED, RunState.FAILED}
)


class SearchRunCreation(Protocol):
    def create_run(self, frozen_spec_id: str, source_run_id: str | None = None,
                   *, run_id: str | None = None) -> str: ...
    def fail_creation(self, run_id: str) -> None: ...


class SearchRunState(Protocol):
    def new_run_id(self, now: float) -> str: ...
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def write_run(self, run: RunRecord) -> None: ...
    def read_plan(self, run_id: str, plan_id: str) -> SearchPlan: ...
    def read_plans(self, run_id: str) -> list[SearchPlan]: ...
    def write_plan(self, plan: SearchPlan) -> None: ...
    def initialize_run_layout(self, run_id: str, workspace_provider: WorkspaceProvider) -> None: ...
    def relocation_log_dir(self, run_id: str) -> Path: ...


class PreparedBaseline(Protocol):
    provider_handle: dict[str, Any]

    def capture(self, run: RunRecord) -> ArtifactRef: ...
    def verify(
        self, run: RunRecord, frozen: FrozenSpec, artifact: ArtifactRef
    ) -> ScoreReport: ...


class BaselinePreparation(Protocol):
    def prepare_baseline(
        self, spec: SearchSpec, *, agent_harness: AgentHarnessKind = "pi"
    ) -> PreparedBaseline: ...


class DeviceLeaseReservation(Protocol):
    def commit(self) -> None: ...


class DeviceLeases(Protocol):
    def reservation(
        self, run: RunRecord, frozen: FrozenSpec
    ) -> AbstractContextManager[DeviceLeaseReservation]: ...
    def release_device_pool_leases(self, run: RunRecord) -> None: ...
