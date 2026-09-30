"""Read-only cross-candidate reference inputs for terminal reports."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.edit_surface import path_matches
from goal_plus.domain.search import FrozenSpec
from goal_plus.infrastructure.state.candidate_trace import FileCandidateTraceStore
from goal_plus.ports.report import ReportReferenceImpact
from goal_plus.ports.state import CandidateRecord, RunRecord


_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")


def _safe_relative_path(value: str) -> str | None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\x00" in value:
        return None
    return path.as_posix()


def _git_output(
    repository: Path, *arguments: str
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", f"--git-dir={repository}", *arguments],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _blob_id(repository: Path, commit: str, path: str) -> str | None:
    result = _git_output(repository, "rev-parse", f"{commit}:{path}")
    if result is None or result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if _GIT_OBJECT_ID.fullmatch(value) else None


def _git_comparison(
    *,
    run_dir: Path,
    run: RunRecord,
    frozen: FrozenSpec,
    source: ArtifactRef,
    target: ArtifactRef,
    files: set[str],
) -> tuple[str, tuple[str, ...], int | None, int | None]:
    if (
        source.provider != "git_worktree"
        or target.provider != "git_worktree"
        or source.kind != "git_commit"
        or target.kind != "git_commit"
        or not _GIT_OBJECT_ID.fullmatch(source.id)
        or not _GIT_OBJECT_ID.fullmatch(target.id)
    ):
        return "unavailable", (), None, None
    repositories = (
        run_dir / "workspace-repository.git",
        run_dir / "workspace-repository" / ".git",
    )
    repository = next((path for path in repositories if path.is_dir()), None)
    if repository is None:
        return "unavailable", (), None, None
    for commit in (source.id, target.id):
        result = _git_output(repository, "cat-file", "-e", f"{commit}^{{commit}}")
        if result is None or result.returncode != 0:
            return "unavailable", (), None, None

    relative_files = []
    for value in sorted(files):
        normalized = _safe_relative_path(value)
        if normalized is None:
            continue
        if not path_matches(normalized, frozen.spec.edit_surface.allow):
            continue
        if path_matches(normalized, frozen.spec.edit_surface.deny):
            continue
        relative_files.append(normalized)
    if not relative_files:
        return "unavailable", (), None, None

    prefix = PurePosixPath(run.source_subdir)
    repository_paths = [
        (prefix / path).as_posix() if prefix.as_posix() != "." else path
        for path in relative_files
    ]
    pairs = [
        (
            _blob_id(repository, source.id, path),
            _blob_id(repository, target.id, path),
        )
        for path in repository_paths
    ]
    if not any(left is not None or right is not None for left, right in pairs):
        return "unavailable", tuple(relative_files), None, None

    exact = all(left == right for left, right in pairs)
    if exact:
        return "exact_editable_content", tuple(relative_files), 0, 0

    result = _git_output(
        repository,
        "diff",
        "--numstat",
        source.id,
        target.id,
        "--",
        *repository_paths,
    )
    if result is None or result.returncode != 0:
        return "different_editable_content", tuple(relative_files), None, None
    added = 0
    deleted = 0
    for line in result.stdout.splitlines():
        columns = line.split("\t", 2)
        if len(columns) != 3 or not columns[0].isdigit() or not columns[1].isdigit():
            return "different_editable_content", tuple(relative_files), None, None
        added += int(columns[0])
        deleted += int(columns[1])
    return "different_editable_content", tuple(relative_files), added, deleted


def collect_reference_impacts(
    root_dir: Path,
    run: RunRecord,
    frozen: FrozenSpec,
    candidates: list[CandidateRecord],
) -> list[ReportReferenceImpact]:
    """Pair every durable reference open with the next settled candidate edit."""
    trace = FileCandidateTraceStore(root_dir)
    run_dir = root_dir / "runs" / run.run_id
    iterations = {
        (candidate.candidate_id, iteration.iteration): iteration
        for candidate in candidates
        for iteration in candidate.iterations
    }
    impacts: list[ReportReferenceImpact] = []
    for candidate in candidates:
        events = trace.audit_events(run.run_id, candidate.candidate_id).events
        settlements = [event for event in events if event.kind == "verifier.settled"]
        references = [event for event in events if event.kind == "reference.opened"]
        for reference in references:
            preceding = next(
                (
                    event
                    for event in reversed(settlements)
                    if event.sequence < reference.sequence
                ),
                None,
            )
            subsequent = next(
                (
                    event
                    for event in settlements
                    if event.sequence > reference.sequence
                ),
                None,
            )
            source_key = (
                reference.payload["source_candidate_id"],
                reference.payload["source_iteration"],
            )
            source_iteration = iterations.get(source_key)
            subsequent_iteration = (
                iterations.get(
                    (candidate.candidate_id, subsequent.payload["iteration"])
                )
                if subsequent is not None
                else None
            )
            source_ref = ArtifactRef.model_validate(reference.payload["artifact_ref"])
            target_ref = (
                ArtifactRef.model_validate(subsequent.payload["attempt_artifact_ref"])
                if subsequent is not None
                else None
            )
            files = (
                set(subsequent.payload.get("attempt_changed_files") or [])
                if subsequent is not None
                else set()
            )
            if source_iteration is not None:
                files.update(source_iteration.attempt_changed_files)
            comparison = (
                _git_comparison(
                    run_dir=run_dir,
                    run=run,
                    frozen=frozen,
                    source=source_ref,
                    target=target_ref,
                    files=files,
                )
                if target_ref is not None
                else ("unavailable", (), None, None)
            )
            impacts.append(
                ReportReferenceImpact(
                    requester_candidate_id=candidate.candidate_id,
                    requester_agent_session_id=reference.agent_session_id,
                    reference_event_id=reference.event_id,
                    opened_at=reference.occurred_at,
                    source_candidate_id=source_key[0],
                    source_iteration=source_key[1],
                    source_artifact_ref=source_ref,
                    source_summary=(
                        source_iteration.summary or source_iteration.hypothesis or None
                        if source_iteration is not None
                        else None
                    ),
                    preceding_iteration=(
                        preceding.payload["iteration"] if preceding is not None else None
                    ),
                    preceding_score=(
                        preceding.payload.get("score") if preceding is not None else None
                    ),
                    subsequent_iteration=(
                        subsequent.payload["iteration"] if subsequent is not None else None
                    ),
                    subsequent_score=(
                        subsequent.payload.get("score") if subsequent is not None else None
                    ),
                    subsequent_artifact_ref=target_ref,
                    subsequent_summary=(
                        subsequent_iteration.summary
                        or subsequent_iteration.hypothesis
                        or None
                        if subsequent_iteration is not None
                        else None
                    ),
                    settlement_event_id=(
                        subsequent.event_id if subsequent is not None else None
                    ),
                    compared_files=comparison[1],
                    comparison_state=comparison[0],
                    added_lines=comparison[2],
                    deleted_lines=comparison[3],
                )
            )
    return sorted(
        impacts,
        key=lambda item: (
            item.opened_at,
            item.requester_candidate_id,
        ),
    )
