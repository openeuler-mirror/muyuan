"""Resolve freeze inputs from separate configuration and source capabilities."""

from __future__ import annotations
import subprocess
import uuid
from pathlib import Path
from goal_plus.domain.search import SearchSpec, VerifierCommand
from goal_plus.ports.search_bootstrap import FreezeInputs
from goal_plus.infrastructure.artifacts.source_repository import SourceRepository
from goal_plus.infrastructure.artifacts.thinkthread_baseline import thinkthread_source_relative_path
from goal_plus.infrastructure.search_configuration import SearchConfiguration, supplemental_evaluation_enabled, supplemental_evaluation_required, SUPPLEMENTAL_EVALUATION_ENABLED_ENV, SUPPLEMENTAL_EVALUATION_REQUIRED_ENV
from goal_plus.infrastructure.state.files import sha256_file
from goal_plus.paths import DEFAULT_RUNTIME_ROOT, LEGACY_RUNTIME_ROOT
from goal_plus.workspaces import IGNORED_NAMES, IGNORED_SUFFIXES, estimate_workspace_capacity, LocalGitWorkspace

def relative_artifact_path(source_root: Path, artifact_path: Path) -> str:
    artifact = artifact_path.resolve()
    try:
        return artifact.relative_to(source_root.resolve()).as_posix()
    except ValueError:
        return artifact.name


TASK_SKILL_DIRECTORY = Path(".agents/skills")


def discover_git_tracked_task_skills(source_root: Path) -> list[str]:
    """Return task-local Skill entrypoints whose complete bundles are Git-tracked."""
    source_root = source_root.resolve()
    if not source_root.is_dir():
        return []
    agents_directory = source_root / ".agents"
    skill_directory = source_root / TASK_SKILL_DIRECTORY
    for directory in (agents_directory, skill_directory):
        if directory.is_symlink():
            raise ValueError(
                "Task-local Skill directory cannot be a symlink: "
                f"{directory.relative_to(source_root).as_posix()}"
            )
    if not skill_directory.exists():
        return []
    if not skill_directory.is_dir():
        raise ValueError(
            f"Task-local Skill path is not a directory: {TASK_SKILL_DIRECTORY}"
        )
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "-z", "--", str(TASK_SKILL_DIRECTORY)],
            cwd=source_root,
            check=False,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Cannot inspect task-local Skills in {source_root}: git was not found"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Cannot inspect task-local Skills in {source_root}: "
            f"git ls-files failed ({detail or f'exit {result.returncode}'})"
        )

    tracked_paths: set[str] = set()
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative_path = raw_path.decode("utf-8", errors="surrogateescape")
        tracked_paths.add(Path(relative_path).as_posix())

    discovered: list[str] = []
    for relative_path in sorted(tracked_paths):
        parts = Path(relative_path).parts
        if (
            len(parts) != 4
            or parts[:2] != (".agents", "skills")
            or parts[-1] != "SKILL.md"
        ):
            continue
        skill_path = source_root / relative_path
        skill_root = skill_path.parent
        if skill_root.is_symlink() or skill_path.is_symlink():
            raise ValueError(
                f"Git-tracked task-local Skill cannot be a symlink: {relative_path}"
            )
        if not skill_path.is_file():
            continue

        untracked_files: list[str] = []
        for bundle_path in skill_root.rglob("*"):
            bundle_relative = bundle_path.relative_to(source_root).as_posix()
            if bundle_path.is_symlink():
                raise ValueError(
                    "Task-local Skill bundle cannot contain a symlink: "
                    f"{bundle_relative}"
                )
            if bundle_path.is_file() and bundle_relative not in tracked_paths:
                untracked_files.append(bundle_relative)
        if untracked_files:
            raise ValueError(
                "Task-local Skill contains files not tracked by Git: "
                + ", ".join(sorted(untracked_files))
            )
        discovered.append(Path(relative_path).as_posix())
    return discovered


def _normalize_verifier_cwds_for_candidate_workspace(spec: SearchSpec) -> SearchSpec:
    source_root = Path(spec.source_path).resolve()

    def normalize_command(command: VerifierCommand) -> VerifierCommand:
        cwd_path = Path(command.cwd)
        if cwd_path.resolve() == source_root:
            return command.model_copy(update={"cwd": "."})
        return command

    return spec.model_copy(
        deep=True,
        update={
            "process_verifiers": [
                normalize_command(command) for command in spec.process_verifiers
            ],
            "promotion_verifiers": [
                normalize_command(command) for command in spec.promotion_verifiers
            ],
        },
    )


class FileFreezeSource:
    def __init__(self, root_dir: Path | str, configuration: SearchConfiguration, repository: SourceRepository) -> None:
        self.root_dir = Path(root_dir)
        self.configuration, self.repository = configuration, repository

    def prepare(self, spec: SearchSpec, verifier_artifacts: list[Path]) -> FreezeInputs:
        supplemental_enabled = supplemental_evaluation_enabled()
        supplemental_required = supplemental_evaluation_required()
        if supplemental_required and not supplemental_enabled:
            raise ValueError(
                f"{SUPPLEMENTAL_EVALUATION_REQUIRED_ENV}=1 requires "
                f"{SUPPLEMENTAL_EVALUATION_ENABLED_ENV}=1"
            )
        spec = self.configuration.normalize_inner_agent_spec(spec)
        self.configuration.validate_host_workspace_backend(spec)
        self.configuration.validate_inner_agent_binding(spec.strategy)
        spec = _normalize_verifier_cwds_for_candidate_workspace(spec)
        spec = self.configuration.apply_global_evidence_mode_from_environment(spec)
        spec = self.configuration.normalize_strategy_models(spec)
        self.configuration.validate_strategy_config(spec.strategy)
        device_inventory = self.configuration.resolve_device_inventory(
            spec.device_pools,
            spec.budget.max_parallel,
        )
        source_root = Path(spec.source_path).resolve()
        self.repository.validate_clean_source_repository(spec)
        if spec.workspace.backend == "thinkthread":
            thinkthread_source_relative_path(source_root)
            if spec.shared_dir.enabled:
                raise ValueError(
                    "ThinkThread Search does not enable shared_dir until provider-native "
                    "tool staging is implemented"
                )
        workspace_capacity_estimate = estimate_workspace_capacity(
            source=source_root,
            backend=spec.workspace.backend,
            worktree_nodes=spec.budget.max_parallel,
            destination_root=self.root_dir,
        )
        verifier_hashes: dict[str, str] = {}
        artifact_entries: list[tuple[Path, str]] = []
        task_skill_paths = discover_git_tracked_task_skills(source_root)

        for artifact in verifier_artifacts:
            artifact_path = Path(artifact).resolve()
            if not artifact_path.exists() or not artifact_path.is_file():
                raise FileNotFoundError(f"verifier artifact not found: {artifact_path}")
            try:
                artifact_path.relative_to(source_root)
            except ValueError as exc:
                raise ValueError(
                    f"Verifier artifact is outside source_path '{source_root}': "
                    f"{artifact_path}. Move it into a source-owned, materialized "
                    "path such as '.goal-plus-verifiers/'."
                ) from exc
            rel_path = relative_artifact_path(source_root, artifact_path)
            ignored_part = next(
                (part for part in Path(rel_path).parts if part in IGNORED_NAMES),
                None,
            )
            if ignored_part is not None:
                if ignored_part in {DEFAULT_RUNTIME_ROOT, LEGACY_RUNTIME_ROOT}:
                    raise ValueError(
                        "Verifier artifact is under the ignored Goal Plus runtime "
                        f"directory '{ignored_part}': {rel_path}. Move it to a "
                        "source-owned path such as "
                        "'.goal-plus-verifiers/score.sh'."
                    )
                raise ValueError(
                    f"Verifier artifact is under ignored workspace path "
                    f"'{ignored_part}': {rel_path}. Move it to a source-owned, "
                    "materialized path."
                )
            if Path(rel_path).suffix in IGNORED_SUFFIXES:
                raise ValueError(
                    f"Verifier artifact uses ignored workspace suffix "
                    f"'{Path(rel_path).suffix}': {rel_path}. Move it to a "
                    "source-owned, materialized path."
                )
            artifact_entries.append((artifact_path, rel_path))

        self.repository.validate_materialized_verifier_artifacts(spec, artifact_entries)
        for artifact_path, rel_path in artifact_entries:
            verifier_hashes[rel_path] = sha256_file(artifact_path)

        source_artifact_ref = (
            LocalGitWorkspace("git_worktree").current_artifact(source_root)
            if spec.workspace.backend == "git_worktree"
            else None
        )
        return FreezeInputs(
            spec,
            source_root,
            supplemental_enabled,
            supplemental_required,
            device_inventory,
            workspace_capacity_estimate,
            verifier_hashes,
            artifact_entries,
            task_skill_paths,
            source_artifact_ref,
            self.root_dir / "bootstrap" / f"origin-{uuid.uuid4().hex[:12]}",
        )
