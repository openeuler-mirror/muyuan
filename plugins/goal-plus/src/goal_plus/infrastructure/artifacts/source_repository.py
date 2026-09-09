"""Read-only source identity, cleanliness and verifier-materialization checks."""

import subprocess
from pathlib import Path
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.search import SearchSpec
from goal_plus.infrastructure.artifacts.git import git_output
from goal_plus.workspaces import LocalGitWorkspace, git_status


class SourceRepository:
    def current_artifact(self, spec: SearchSpec) -> ArtifactRef | None:
        return LocalGitWorkspace(spec.workspace.backend).current_artifact(
            Path(spec.source_path)
        )

    def git_status(
        self,
        workspace: Path,
        *,
        ignore_runtime_noise: bool = False,
        strict: bool = False,
    ) -> list[str]:
        try:
            return git_status(workspace, ignore_runtime_noise=ignore_runtime_noise)
        except RuntimeError as exc:
            if strict:
                raise RuntimeError(
                    "SourceGitPreflightFailed: could not inspect the source Git "
                    f"repository at {workspace}: {exc}"
                ) from exc
            return []

    def validate_clean_source_repository(self, spec: SearchSpec) -> None:
        if spec.workspace.backend != "git_worktree":
            return

        source = Path(spec.source_path).resolve()
        probe = source if source.is_dir() else source.parent
        try:
            repository_text = git_output(
                probe,
                ["git", "rev-parse", "--show-toplevel"],
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "SourceGitPreflightFailed: Git is required for workspace backend "
                f"{spec.workspace.backend!r}"
            ) from exc
        except subprocess.CalledProcessError:
            # Plain directories keep the existing synthetic Git baseline path.
            return

        repository = Path(repository_text.strip()).resolve()
        status = self.git_status(
            repository,
            ignore_runtime_noise=True,
            strict=True,
        )
        if not status:
            return

        preview_limit = 10
        preview = "; ".join(status[:preview_limit])
        remaining = len(status) - preview_limit
        suffix = f"; ... and {remaining} more" if remaining > 0 else ""
        raise ValueError(
            "DirtySourceWorkspace: workspace backend "
            f"{spec.workspace.backend!r} requires a clean Git HEAD, but "
            f"{repository} has {len(status)} staged, unstaged, or non-ignored "
            f"untracked path(s): {preview}{suffix}. Commit, stash, remove, or "
            "ignore those paths before freezing the SearchSpec. To intentionally "
            "snapshot the current filesystem, use workspace.backend='copy' with "
            "promotion_mode='artifact_only'."
        )

    def validate_materialized_verifier_artifacts(
        self,
        spec: SearchSpec,
        artifact_entries: list[tuple[Path, str]],
    ) -> None:
        if spec.workspace.backend != "git_worktree":
            return
        source = Path(spec.source_path).resolve()
        try:
            repository = Path(
                git_output(source, ["git", "rev-parse", "--show-toplevel"]).strip()
            ).resolve()
            source_prefix = source.relative_to(repository)
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
            # Preserve the compatibility path for plain source directories;
            # the workspace provider owns their synthetic Git baseline.
            return
        for _artifact_path, rel_path in artifact_entries:
            object_path = (source_prefix / rel_path).as_posix()
            try:
                git_output(
                    repository,
                    ["git", "cat-file", "-e", f"HEAD:{object_path}"],
                )
            except subprocess.CalledProcessError as exc:
                raise ValueError(
                    "VerifierArtifactNotMaterialized: verifier artifact "
                    f"{rel_path!r} is present in the live filesystem but absent "
                    "from the frozen source HEAD. Commit the artifact in the "
                    "standalone source repository before freezing."
                ) from exc

    @staticmethod
    def resolved_source_path(spec: SearchSpec) -> str:
        return str(Path(spec.source_path).resolve())
