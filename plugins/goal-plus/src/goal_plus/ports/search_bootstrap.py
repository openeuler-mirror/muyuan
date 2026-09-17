"""Resolved source inputs and narrow bootstrap verification/persistence ports."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from goal_plus.domain.agents import AgentHarnessKind, AgentRuntimeProvider

from goal_plus.domain.artifacts import (
    ArtifactRef,
    WorkspaceProvider,
    WorkspaceCapacityEstimate,
)
from goal_plus.domain.search import (
    BootstrapVerification,
    DevicePoolInventory,
    FrozenSpec,
    SearchSpec,
)


@dataclass(frozen=True)
class FreezeInputs:
    runtime_provider: AgentRuntimeProvider
    spec: SearchSpec
    source_root: Path
    supplemental_enabled: bool
    supplemental_required: bool
    device_inventory: dict[str, DevicePoolInventory]
    workspace_capacity_estimate: WorkspaceCapacityEstimate
    verifier_hashes: dict[str, str]
    artifact_entries: list[tuple[Path, str]]
    task_skill_paths: list[str]
    source_artifact_ref: ArtifactRef | None
    origin_log_dir: Path


class FreezeSource(Protocol):
    def prepare(
        self, spec: SearchSpec, verifier_artifacts: list[Path], *, agent_harness: AgentHarnessKind
    ) -> FreezeInputs: ...


class SourceInspection(Protocol):
    def validate_clean_source_repository(self, spec: SearchSpec) -> None: ...
    def current_artifact(self, spec: SearchSpec) -> ArtifactRef | None: ...
    def resolved_source_path(self, spec: SearchSpec) -> str: ...


class BootstrapVerifiers(Protocol):
    def run_bootstrap_verifiers(
        self,
        *,
        spec: SearchSpec,
        workspace: Path,
        workspace_provider: WorkspaceProvider,
        phase: Literal["origin", "workspace_relocation"],
        verifier_hashes: dict[str, str],
        log_dir: Path,
        source_artifact_ref: ArtifactRef | None,
        device_inventory: dict[str, DevicePoolInventory],
        candidate_id: str | None = None,
    ) -> BootstrapVerification: ...
    def preflight_process_verifiers(
        self,
        spec: SearchSpec,
        device_inventory: dict[str, DevicePoolInventory],
    ) -> BootstrapVerification: ...


class FrozenSpecState(Protocol):
    def copy_verifiers(
        self, frozen_spec_id: str, entries: list[tuple[Path, str]]
    ) -> dict[str, str]: ...
    def write(self, frozen: FrozenSpec) -> None: ...
