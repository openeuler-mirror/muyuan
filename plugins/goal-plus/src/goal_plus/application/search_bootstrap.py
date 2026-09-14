"""Freeze one immutable Search contract from resolved source and verifier evidence."""

from pathlib import Path

from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.domain.agents import AgentHarnessKind
from goal_plus.domain.search import BootstrapVerification, FrozenSpec, SearchSpec
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.goal_authorization import validate_search_command_config
from goal_plus.ports.goal_authorization import GoalToolTarget
from goal_plus.ports.clock import Clock
from goal_plus.ports.search_bootstrap import (
    BootstrapVerifiers,
    FreezeSource,
    FrozenSpecState,
    SourceInspection,
)


class SearchBootstrap:
    def __init__(
        self,
        source: FreezeSource,
        state: FrozenSpecState,
        verifiers: BootstrapVerifiers,
        clock: Clock,
        repository: SourceInspection,
        authorization: GoalAuthorization,
    ) -> None:
        self.source, self.state, self.verifiers, self.clock = (
            source,
            state,
            verifiers,
            clock,
        )
        self.repository = repository
        self.authorization = authorization

    def freeze(
        self,
        spec: SearchSpec,
        verifier_artifacts: list[Path],
        *,
        goal_plus_id: str | None = None,
        agent_harness: AgentHarnessKind | None = None,
    ) -> FrozenSpec:
        goal_revision = None
        if goal_plus_id is not None:
            goal = self.authorization.require_active_target(
                GoalToolTarget("goal_plus_search_freeze_spec", goal_plus_id=goal_plus_id)
            )
            validate_search_command_config(goal.command_config, spec)
            goal_revision = goal.goal_revision
            goal_host = self.authorization.search_agent_harness(goal_plus_id)
            if agent_harness is not None and agent_harness != goal_host:
                raise ValueError("Search native host must match its Main Goal session")
            agent_harness = goal_host
        if agent_harness is None:
            raise ValueError("Search freeze requires a trusted native host")
        inputs = self.source.prepare(spec, verifier_artifacts, agent_harness=agent_harness)
        spec, source_root = inputs.spec, inputs.source_root
        supplemental_enabled, supplemental_required = (
            inputs.supplemental_enabled,
            inputs.supplemental_required,
        )
        device_inventory, verifier_hashes = (
            inputs.device_inventory,
            inputs.verifier_hashes,
        )
        task_skill_paths, artifact_entries = (
            inputs.task_skill_paths,
            inputs.artifact_entries,
        )
        workspace_capacity_estimate = inputs.workspace_capacity_estimate
        source_artifact_ref = inputs.source_artifact_ref
        origin_verification: BootstrapVerification | None = None
        if spec.workspace.provider == "git_worktree":
            origin_verification = self.verifiers.run_bootstrap_verifiers(
                spec=spec,
                workspace=source_root,
                workspace_provider=spec.workspace.provider,
                phase="origin",
                verifier_hashes=verifier_hashes,
                log_dir=inputs.origin_log_dir,
                source_artifact_ref=source_artifact_ref,
                device_inventory=device_inventory,
            )
            if not origin_verification.passed:
                failed = next(
                    (
                        result
                        for result in origin_verification.verifier_results
                        if not result.passed
                    ),
                    None,
                )
                detail = (
                    f" {failed.name}: {failed.failure_class}."
                    if failed is not None
                    else ""
                )
                raise ValueError(
                    "OriginVerifierFailed: process verifier suite did not pass "
                    f"in source_path '{source_root}'.{detail}"
                )
            origin_verification.require_numeric_metric(spec.metric_name)
            self.repository.validate_clean_source_repository(spec)
            source_artifact_after = self.repository.current_artifact(spec)
            if source_artifact_after != source_artifact_ref:
                raise ValueError(
                    "OriginArtifactChanged: source artifact changed while origin "
                    "verification was running"
                )
            origin_verification = origin_verification.model_copy(
                update={"source_artifact_ref": source_artifact_after}
            )
            source_artifact_ref = source_artifact_after
        elif spec.workspace.provider != "thinkthread":
            self.verifiers.preflight_ranking_verifiers(spec, device_inventory)

        spec_payload = spec.model_dump(mode="json")
        device_inventory_payload = {
            pool_id: inventory.model_dump(mode="json")
            for pool_id, inventory in device_inventory.items()
        }
        spec_hash = sha256_text(
            canonical_json(
                {
                    "goal_plus_id": goal_plus_id,
                    "goal_revision": goal_revision,
                    "agent_harness": agent_harness,
                    "runtime_provider": inputs.runtime_provider,
                    "spec": spec_payload,
                    "verifiers": verifier_hashes,
                    "source_artifact_ref": (
                        source_artifact_ref.model_dump(mode="json")
                        if source_artifact_ref is not None
                        else None
                    ),
                    "task_skills": task_skill_paths,
                    "device_inventory": device_inventory_payload,
                    "supplemental_evaluation_enabled": supplemental_enabled,
                    "supplemental_evaluation_required": supplemental_required,
                }
            )
        )
        frozen_spec_id = f"spec_{spec_hash[:12]}"
        frozen_verifier_paths = self.state.copy_verifiers(
            frozen_spec_id, artifact_entries
        )

        frozen = FrozenSpec(
            frozen_spec_id=frozen_spec_id,
            spec_hash=spec_hash,
            goal_plus_id=goal_plus_id,
            goal_revision=goal_revision,
            agent_harness=agent_harness,
            runtime_provider=inputs.runtime_provider,
            spec=spec,
            supplemental_evaluation_enabled=supplemental_enabled,
            supplemental_evaluation_required=supplemental_required,
            verifier_hashes=verifier_hashes,
            frozen_verifier_paths=frozen_verifier_paths,
            task_skill_paths=task_skill_paths,
            workspace_capacity_estimate=workspace_capacity_estimate,
            source_artifact_ref=source_artifact_ref,
            origin_verification=origin_verification,
            device_inventory=device_inventory,
            created_at=self.clock.timestamp(),
        )
        self.state.write(frozen)
        return frozen
