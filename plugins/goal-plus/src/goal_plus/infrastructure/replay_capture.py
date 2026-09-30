"""Capture an immutable, experiment-only candidate state at an Evidence return."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from typing import Any

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.replay import ReplayForkPoint, ReplayTarget
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.infrastructure.state.annotation import FileAnnotationState
from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.infrastructure.state.candidate_trace import FileCandidateTraceStore
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json
from goal_plus.infrastructure.state.goal import FileGoalQueries
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.runtime import FileSearchRuntime
from goal_plus.host_scope import execution_scope_for_run
from goal_plus.workspaces import copy_source_tree


def workspace_digest(root: Path) -> str:
    """Hash file bytes, modes and symlink targets, excluding Git control files."""
    digest = hashlib.sha256()

    def update_part(part: bytes) -> None:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)

    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name != ".git")
        for name in sorted([*directories, *filenames]):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if name == ".git" or relative == ".git":
                continue
            stat = path.lstat()
            if path.is_symlink():
                if not path.resolve().is_relative_to(root.resolve()):
                    raise RuntimeError("ReplayWorkspaceExternalSymlink")
                kind, content = b"link", os.readlink(path).encode()
            elif path.is_dir():
                kind, content = b"dir", b""
            elif path.is_file():
                kind, content = b"file", None
            else:
                raise RuntimeError("ReplayWorkspaceUnsupportedFile")
            for part in (relative.encode(), kind, str(stat.st_mode & 0o777).encode()):
                update_part(part)
            if content is None:
                digest.update(stat.st_size.to_bytes(8, "big"))
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                update_part(content)
    return digest.hexdigest()


class FileReplayCapture:
    def __init__(self, root_dir: Path | str) -> None:
        self.root = Path(root_dir).resolve()
        self.queries = FileCandidateQueries(self.root)
        self.trace = FileCandidateTraceStore(self.root)
        self.annotation = FileAnnotationState(self.root)
        self.goals = FileGoalQueries(self.root)
        self.base = self.root / "replay" / "fork-points"

    def capture_tool_result(
        self,
        *,
        agent_session_id: str,
        tool_name: str,
        tool_result: Any,
        native_thread_id: str,
        native_turn_id: str,
        native_tool_call_id: str,
        remaining_seconds: float | None = None,
    ) -> ReplayForkPoint | None:
        if tool_name == "goal_plus_search_get_global_evidence":
            view, source = tool_result, "explicit"
        elif tool_name == "goal_plus_search_run_verifier" and isinstance(tool_result, dict):
            view = tool_result.get("global_evidence_snapshot")
            source = "verifier_injected"
        else:
            return None
        if not isinstance(view, list) or not view:
            return None
        fork_id = f"fork_{uuid.uuid4().hex}"
        staging = self.base / f".{fork_id}.tmp"
        destination = self.base / fork_id
        try:
            point = self._capture(
                staging, fork_id, agent_session_id, view, source,
                native_thread_id, native_turn_id, native_tool_call_id,
                remaining_seconds,
            )
            staging.replace(destination)
            return point
        except Exception as exc:
            try:
                failed_session = self.queries.read_agent_session(agent_session_id)
                identity = {
                    "run_id": failed_session.run_id,
                    "candidate_id": failed_session.candidate_id,
                }
            except (OSError, ValueError, RuntimeError):
                identity = {}
            write_json(
                self.base / "failures" / f"{fork_id}.json",
                {
                    "fork_point_id": fork_id,
                    "agent_session_id": agent_session_id,
                    "native_thread_id": native_thread_id,
                    "native_turn_id": native_turn_id,
                    **identity,
                    "error_category": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            return None
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _capture(
        self,
        staging: Path,
        fork_id: str,
        agent_session_id: str,
        view: list[Any],
        source: str,
        native_thread_id: str,
        native_turn_id: str,
        native_tool_call_id: str,
        remaining_seconds: float | None,
    ) -> ReplayForkPoint:
        session = self.queries.read_agent_session(agent_session_id)
        with exclusive_file_lock(self.queries.run_dir(session.run_id) / "run.lock"):
            session = self.queries.read_agent_session(agent_session_id, session.run_id)
            run = self.queries.read_run(session.run_id)
            candidate = self.queries.read_candidate(session.run_id, session.candidate_id)
            self.queries.read_frozen_spec(run.frozen_spec_id)
            if (
                session.agent_harness != "codex"
                or session.runtime_provider != "direct"
                or candidate.task.workspace_provider not in {"git_worktree", "copy"}
                or candidate.verification is not None
                or candidate.execution_generation != session.execution_generation
            ):
                raise RuntimeError("UnsupportedReplayProviderOrCandidateState")
            read = next(
                (
                    item for item in reversed(session.global_evidence_reads)
                    if item.source == source
                    and item.snapshot_id == sha256_text(canonical_json(view))
                ),
                None,
            )
            if read is None:
                raise RuntimeError("ReplayGlobalEvidenceReceiptMissing")
            events = [
                event for event in self.trace.read_events(session.run_id, session.candidate_id)
                if event.kind == "evidence.global_returned"
                and event.operation_id == read.operation_id
                and event.payload["snapshot_id"] == read.snapshot_id
            ]
            if len(events) != 1:
                raise RuntimeError("ReplayGlobalEvidenceTraceMismatch")
            targets: list[ReplayTarget] = []
            referenced_artifacts: dict[str, set[str]] = {}
            for entry in view:
                if isinstance(entry, dict) and entry.get("reference_available") is not False:
                    raw_artifact = entry.get("artifact_ref")
                    if isinstance(raw_artifact, dict):
                        reference_artifact = ArtifactRef.model_validate(raw_artifact)
                        if (
                            reference_artifact.kind == "git_commit"
                            and reference_artifact.provider in {"copy", "git_worktree"}
                        ):
                            referenced_artifacts.setdefault(
                                str(entry["candidate_id"]), set(),
                            ).add(reference_artifact.id)
                if not isinstance(entry, dict) or not entry.get("supplemental_available"):
                    continue
                artifact = ArtifactRef.model_validate(entry["artifact_ref"])
                candidate_id, iteration = str(entry["candidate_id"]), int(entry["iteration"])
                task = self.annotation.read_task(session.run_id, candidate_id, iteration)
                if (
                    task is None or task.state != "completed" or task.view is None
                    or task.attempt_artifact_ref != artifact
                    or task.attempt_commit != entry.get("commit")
                    or task.view.supplemental_evaluation is None
                ):
                    continue
                detail = {
                    "candidate_id": candidate_id,
                    "iteration": iteration,
                    "commit": entry.get("commit"),
                    "supplemental_evaluation": task.view.supplemental_evaluation.model_dump(mode="json"),
                }
                detail_hash = sha256_text(canonical_json(detail))
                write_json(staging / "details" / f"{detail_hash}.json", detail)
                targets.append(ReplayTarget(
                    candidate_id=candidate_id, iteration=iteration,
                    artifact_ref=artifact, detail_snapshot_id=detail_hash,
                ))
            if not targets:
                raise RuntimeError("ReplayNoAvailableEvidenceDetail")
            configured = (session.launch.get("budget_control") or {}).get("max_runtime_seconds")
            deadline = outer_deadline_epoch(
                FileSearchRuntime(self.root).candidate_sessions.context.outer_deadline(run.run_id)
            )
            remaining = min(
                int(configured) if configured is not None else 3600,
                int(deadline - datetime.now(timezone.utc).timestamp()) if deadline is not None else 3600,
                int(remaining_seconds) if remaining_seconds is not None else 3600,
            )
            if remaining <= 0:
                raise RuntimeError("ReplayRuntimeBudgetExhausted")
            scope = execution_scope_for_run(root_dir=self.root, run_id=run.run_id)
            native_config = (scope or {}).get("native_agent_config") or {}
            model = session.launch.get("model") or native_config.get("model")
            if not isinstance(model, str) or not model:
                raise RuntimeError("ReplayModelIdentityUnavailable")
            launch = {**session.launch, "model": model}
            if "reasoning_effort" not in launch and native_config.get("reasoning_effort"):
                launch["reasoning_effort"] = native_config["reasoning_effort"]
            managed = candidate.task.workspace_managed_root or candidate.task.workspace
            if not managed.is_dir() or not candidate.task.workspace.resolve().is_relative_to(managed.resolve()):
                raise RuntimeError("ReplayWorkspaceMismatch")
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=managed, text=True
            ).strip()
            before = workspace_digest(managed)
            staging.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                managed, staging / "workspace-files", symlinks=True,
                ignore=lambda _path, names: {".git"} & set(names),
            )
            if before != workspace_digest(staging / "workspace-files") or before != workspace_digest(managed):
                raise RuntimeError("ReplayWorkspaceChangedDuringCapture")
            source_run = self.queries.run_dir(session.run_id)
            def ignore_run(path: str, names: list[str]) -> set[str]:
                return (
                    {
                        "reference-workspaces", "workspace",
                        "workspace-repository", "workspace-repository.git",
                    } & set(names)
                ) if Path(path) == source_run else set()
            shutil.copytree(source_run, staging / "run", ignore=ignore_run, symlinks=True)

            for path in sorted((source_run / "references").glob("ref_*.json")):
                reference = load_json(path)
                raw_artifact = reference.get("artifact_ref")
                if reference.get("state") != "open" or not isinstance(raw_artifact, dict):
                    continue
                artifact = ArtifactRef.model_validate(raw_artifact)
                if artifact.kind == "git_commit" and artifact.provider in {"copy", "git_worktree"}:
                    referenced_artifacts.setdefault(
                        str(reference["source_candidate_id"]), set(),
                    ).add(artifact.id)
            referenced_artifacts.setdefault(candidate.candidate_id, set()).add(head)

            def candidate_repository(record) -> Path:
                candidate_managed = record.task.workspace_managed_root or record.task.workspace
                if (
                    not candidate_managed.is_dir()
                    or not record.task.workspace.resolve().is_relative_to(candidate_managed.resolve())
                ):
                    raise RuntimeError("ReplayWorkspaceMismatch")
                if record.task.workspace_provider == "copy":
                    return candidate_managed
                handle_repository = record.task.workspace_provider_handle.get("repository")
                options = [
                    Path(handle_repository) if isinstance(handle_repository, str) else None,
                    source_run / "workspace-repository.git",
                    source_run / "workspace-repository",
                ]
                return next(
                    (item for item in options if item is not None and item.is_dir()),
                    source_run / ".missing-replay-repository",
                )

            repository = candidate_repository(candidate)
            if not repository.is_dir():
                raise RuntimeError("ReplayGitRepositoryUnavailable")
            subprocess.run(
                ["git", "clone", "--mirror", "--no-hardlinks", str(repository), str(staging / "repository.git")],
                check=True, capture_output=True, text=True,
            )
            captured_repositories = staging / "candidate-repositories"
            captured_repositories.mkdir()
            (captured_repositories / f"{candidate.candidate_id}.git").symlink_to(
                Path("..") / "repository.git", target_is_directory=True,
            )
            repository_manifest: dict[str, Any] = {
                candidate.candidate_id: {
                    "provider": candidate.task.workspace_provider,
                    "workspace_head": head,
                },
            }
            for referenced_candidate_id, commits in sorted(referenced_artifacts.items()):
                referenced_candidate = self.queries.read_candidate(
                    session.run_id, referenced_candidate_id,
                )
                if referenced_candidate.task.workspace_provider not in {"copy", "git_worktree"}:
                    raise RuntimeError("ReplayGitRepositoryUnavailable")
                referenced_managed = (
                    referenced_candidate.task.workspace_managed_root
                    or referenced_candidate.task.workspace
                )
                referenced_head = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=referenced_managed, text=True,
                ).strip()
                captured = captured_repositories / f"{referenced_candidate_id}.git"
                if referenced_candidate_id != candidate.candidate_id:
                    referenced_repository = candidate_repository(referenced_candidate)
                    if not referenced_repository.is_dir():
                        raise RuntimeError("ReplayGitRepositoryUnavailable")
                    subprocess.run(
                        [
                            "git", "clone", "--mirror", "--no-hardlinks",
                            str(referenced_repository), str(captured),
                        ],
                        check=True, capture_output=True, text=True,
                    )
                for commit in sorted(commits):
                    result = subprocess.run(
                        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                        cwd=captured, capture_output=True, text=True,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"ReplayEvidenceCommitUnavailable:{referenced_candidate_id}:{commit}"
                        )
                repository_manifest[referenced_candidate_id] = {
                    "provider": referenced_candidate.task.workspace_provider,
                    "workspace_head": referenced_head,
                }
            write_json(captured_repositories / "manifest.json", {
                "schema_version": 1,
                "candidates": repository_manifest,
            })
            shutil.copytree(self.root / "specs" / run.frozen_spec_id, staging / "spec")
            if candidate.task.workspace_provider == "copy":
                copy_source_tree(Path(run.source_path), staging / "baseline-source")
            linked_goals = [
                goal_id for goal_id in self.goals.goal_ids()
                if any(
                    link.run_id == run.run_id
                    for link in self.goals.read_goal(goal_id).search_tasks
                )
            ]
            if len(linked_goals) != 1:
                raise RuntimeError("ReplayLinkedGoalMissingOrAmbiguous")
            shutil.copytree(self.goals.goals_dir / linked_goals[0], staging / "goal")
            write_json(staging / "global-evidence.json", view)
            point = ReplayForkPoint(
                fork_point_id=fork_id, run_id=run.run_id, source_root=str(self.root),
                candidate_id=candidate.candidate_id,
                agent_session_id=agent_session_id,
                execution_generation=session.execution_generation,
                source_iteration_count=len(candidate.iterations),
                frozen_spec_id=run.frozen_spec_id,
                native_thread_id=native_thread_id,
                native_turn_id=native_turn_id,
                native_tool_call_id=native_tool_call_id,
                source=source, operation_id=read.operation_id,
                global_snapshot_id=read.snapshot_id,
                workspace_provider=candidate.task.workspace_provider,
                workspace_head=head, workspace_digest=before,
                run_digest=sha256_text(canonical_json(run.model_dump(mode="json"))),
                created_at=datetime.now(timezone.utc).isoformat(),
                targets=targets, launch=launch,
                remaining_runtime_seconds=remaining,
            )
            write_json(staging / "manifest.json", point.model_dump(mode="json"))
            return point

    def read(self, fork_point_id: str) -> ReplayForkPoint:
        if not fork_point_id.startswith("fork_") or len(fork_point_id) != 37:
            raise ValueError("invalid ForkPoint identity")
        return ReplayForkPoint.model_validate(
            load_json(self.base / fork_point_id / "manifest.json")
        )

    def list(self, run_id: str, candidate_id: str) -> list[ReplayForkPoint]:
        points = []
        for path in sorted(self.base.glob("fork_*/manifest.json")):
            point = ReplayForkPoint.model_validate(load_json(path))
            if point.run_id == run_id and point.candidate_id == candidate_id:
                points.append(point)
        return points
