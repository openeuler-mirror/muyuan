"""Local immutable patches and ThinkThread publication I/O."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from goal_plus.infrastructure.artifacts.git import git_output
from goal_plus.workspaces import (
    GitPublication,
    UnsupportedWorkspaceCapability,
    copy_source_tree,
    initialize_workspace_git_baseline,
)


class LocalPublication:
    def __init__(self, root_dir: Path | str):
        self.root_dir = Path(root_dir)

    def patch_path(self, run_id, candidate_id):
        directory = self.root_dir / "runs" / run_id / "promotion"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{candidate_id}.patch"

    def native_promotion(self, run, record):
        return bool(
            run.source_artifact_ref is not None
            and record.task.workspace_backend == "git_worktree"
            and run.source_repository is not None
        )

    def prepare_local(self, run, record, selected, changed):
        if record.task.workspace_backend != "git_worktree":
            raise UnsupportedWorkspaceCapability(record.task.workspace_backend, "prepare_promotion")
        return GitPublication().prepare_promotion(
            run_dir=self.root_dir / "runs" / run.run_id,
            run_id=run.run_id,
            candidate_id=record.candidate_id,
            source=Path(run.source_path),
            workspace=record.task.workspace,
            source_subdir=run.source_subdir,
            base_artifact_ref=run.source_artifact_ref,
            selected_artifact_ref=selected,
            changed_files=changed,
        )

    def apply_local(self, prepared):
        if prepared.provider != "git_worktree":
            raise UnsupportedWorkspaceCapability(prepared.provider, "apply_promotion")
        return GitPublication().apply_promotion(prepared)

    def apply_coordinates(self, source_path: Path) -> tuple[Path, Path, str]:
        source_path = source_path.resolve()
        source_root = source_path if source_path.is_dir() else source_path.parent
        try:
            output = git_output(
                source_root,
                ["git", "rev-parse", "--show-toplevel"],
            ).strip()
            if not output:
                raise ValueError("Git returned an empty repository root")
            apply_cwd = Path(output).resolve()
            apply_directory = source_root.relative_to(apply_cwd).as_posix()
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
            apply_cwd = source_root
            apply_directory = "."
        return source_path, apply_cwd, apply_directory

    def write_patch(
        self,
        source: Path,
        workspace: Path,
        selected_revision: str,
        changed_files: list[str],
        patch_path: Path,
    ) -> None:
        if not changed_files:
            patch_path.write_text("", encoding="utf-8")
            return

        with tempfile.TemporaryDirectory(prefix="goal-plus-patch-") as temporary:
            repository = Path(temporary) / "repository"
            copy_source_tree(source, repository)
            baseline = initialize_workspace_git_baseline(repository)
            if baseline is None:
                raise RuntimeError("cannot initialize temporary promotion repository")
            for rel_path in changed_files:
                staged = repository / rel_path
                if staged.exists() or staged.is_symlink():
                    if staged.is_dir() and not staged.is_symlink():
                        shutil.rmtree(staged)
                    else:
                        staged.unlink()
                entry = self._git_tree_entry(
                    workspace,
                    selected_revision,
                    rel_path,
                )
                if entry is None:
                    continue
                mode, object_type, object_id = entry
                if object_type == "tree":
                    staged.mkdir(parents=True, exist_ok=True)
                    continue
                if object_type != "blob":
                    raise RuntimeError(
                        "selected promotion revision contains unsupported Git "
                        f"object type {object_type!r} at {rel_path}"
                    )
                content = self._git_blob(workspace, object_id)
                staged.parent.mkdir(parents=True, exist_ok=True)
                if mode == "120000":
                    staged.symlink_to(os.fsdecode(content))
                else:
                    staged.write_bytes(content)
                    staged.chmod(int(mode, 8) & 0o777)
            git_output(
                repository,
                ["git", "add", "-A", "--", *changed_files],
            )
            patch = self._git_diff(
                repository,
                baseline,
                changed_files,
                cached=True,
            )
        patch_path.write_text(patch, encoding="utf-8")

    def _git_tree_entry(
        self,
        repository: Path,
        revision: str,
        rel_path: str,
    ) -> tuple[str, str, str] | None:
        command = [
            "git",
            "--no-replace-objects",
            "ls-tree",
            "-z",
            revision,
            "--",
            f":(literal){rel_path}",
        ]
        try:
            process = subprocess.run(
                command,
                cwd=repository,
                check=True,
                capture_output=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", b"")
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"failed to read immutable promotion revision: {str(detail).strip()}"
            ) from exc
        if not process.stdout:
            return None
        entries = [entry for entry in process.stdout.split(b"\0") if entry]
        if len(entries) != 1:
            raise RuntimeError(
                f"selected promotion revision has ambiguous path {rel_path!r}"
            )
        metadata, _path = entries[0].split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        return mode, object_type, object_id

    def _git_blob(self, repository: Path, object_id: str) -> bytes:
        command = ["git", "--no-replace-objects", "cat-file", "blob", object_id]
        try:
            return subprocess.run(
                command,
                cwd=repository,
                check=True,
                capture_output=True,
            ).stdout
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", b"")
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"failed to read immutable promotion blob: {str(detail).strip()}"
            ) from exc

    def _git_diff(
        self,
        repository: Path,
        baseline: str,
        changed_files: list[str],
        *,
        cached: bool,
    ) -> str:
        command = [
            "git",
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
        ]
        if cached:
            command.append("--cached")
        command.extend([baseline, "--", *changed_files])
        try:
            return subprocess.run(
                command,
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise RuntimeError(
                f"failed to generate promotion patch: {detail.strip()}"
            ) from exc
