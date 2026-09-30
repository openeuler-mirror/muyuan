"""Offline, isolated Evidence prefetch experiments for Codex direct candidates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from typing import Any

from goal_plus.application.candidate_delivery import CandidateDelivery
from goal_plus.candidate_host import candidate_host_queries
from goal_plus.codex_hook_input import CODEX_CANDIDATE_SESSION_ENV
from goal_plus.domain.replay import ReplayBranch, ReplayExperiment, ReplayForkPoint, ReplayTarget
from goal_plus.ports.state import CandidateRecord, RunRecord
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.execution.native_session import CodexSession
from goal_plus.execution.session_rpc import SessionRpcProcess
from goal_plus.infrastructure.artifacts.references import ProviderReferenceWorkspace
from goal_plus.infrastructure.replay_capture import FileReplayCapture, workspace_digest
from goal_plus.infrastructure.replay_native import verify_injected_prefix
from goal_plus.infrastructure.process_output import TERM_GRACE_SECONDS
from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json
from goal_plus.infrastructure.state.references import FileReferenceState
from goal_plus.mcp_client import McpSessionToolBridge
from goal_plus.ports.process import ProcessRegistration
from goal_plus.ports.session import NativeSessionInput, SessionProcessInput
from goal_plus.runtime import FileSearchRuntime


def _registration(root: Path) -> ProcessRegistration:
    directory = root / "host-sessions" / "replay-processes"

    def register(pid: int) -> str:
        token = uuid.uuid4().hex
        write_json(directory / f"{token}.json", {"pid": pid, "status": "running"})
        return token

    def finish(token: str) -> None:
        path = directory / f"{token}.json"
        record = load_json(path)
        write_json(path, {**record, "status": "finished"})

    return ProcessRegistration(register=register, finish=finish)


def _run_git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _rewrite(value: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        for old, new in replacements:
            if value == old or value.startswith(old + os.sep):
                return new + value[len(old):]
        return value
    if isinstance(value, list):
        return [_rewrite(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, replacements) for key, item in value.items()}
    return value


def _native_input(root: Path, workspace: Path, point: ReplayForkPoint, thread_id: str | None) -> NativeSessionInput:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GOAL_PLUS_") and key != "CODEX_THREAD_ID"
    }
    environment.update(candidate_host_queries(root).process_environment(point.agent_session_id))
    environment.update({
        "GOAL_PLUS_ROOT": str(root), "GOAL_PLUS_SEARCH_ROOT": str(root),
        CODEX_CANDIDATE_SESSION_ENV: point.agent_session_id,
    })
    configuration: dict[str, Any] = {"sandbox_mode": "workspace-write"}
    if point.launch.get("service_tier"):
        configuration["service_tier"] = point.launch["service_tier"]
    return NativeSessionInput(
        agent_harness="codex", runtime_provider="direct",
        process=SessionProcessInput(("codex", "app-server", "--stdio"), workspace, environment),
        model=point.launch["model"], reasoning_effort=point.launch.get("reasoning_effort"),
        native_session_id=thread_id, configuration=configuration,
    )


def _rpc_request(rpc: SessionRpcProcess, method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = rpc.request({"method": method, "params": params}, timeout=60)
    if "error" in response:
        raise RuntimeError(f"{method}: {response['error']}")
    return response["result"]


def _fork_native(point: ReplayForkPoint, root: Path, workspace: Path, prefix: list[Any]) -> str:
    config = _native_input(root, workspace, point, None)
    rpc = SessionRpcProcess(config.process, _registration(root), lambda _event: None)
    try:
        _rpc_request(rpc, "initialize", {
            "clientInfo": {"name": "goal_plus_replay", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        rpc.send({"method": "initialized", "params": {}}, timeout=60)
        fork = _rpc_request(rpc, "thread/fork", {
            "threadId": point.native_thread_id,
            "beforeTurnId": point.native_turn_id,
            "cwd": str(workspace),
            "runtimeWorkspaceRoots": [str(workspace)],
            "model": config.model,
            "config": config.configuration,
            "approvalPolicy": "never",
        })
        thread_id = str(fork["thread"]["id"])
        if thread_id == point.native_thread_id:
            raise RuntimeError("NativeForkReusedSourceThread")
        _rpc_request(rpc, "thread/inject_items", {"threadId": thread_id, "items": prefix})
        read = _rpc_request(rpc, "thread/read", {"threadId": thread_id, "includeTurns": True})
        if read["thread"]["id"] != thread_id:
            raise RuntimeError("NativeForkReadbackIdentityMismatch")
        verify_injected_prefix(thread_id, prefix)
        return thread_id
    finally:
        rpc.close()


class FileReplayExperiments:
    def __init__(self, root_dir: Path | str) -> None:
        self.root = Path(root_dir).resolve()
        self.capture = FileReplayCapture(self.root)
        self.base = self.root / "replay" / "experiments"

    def inspect(self, run_id: str, candidate_id: str) -> list[dict[str, Any]]:
        FileCandidateQueries(self.root).read_candidate(run_id, candidate_id)
        points = self.capture.list(run_id, candidate_id)
        results = [
            {
                **point.model_dump(mode="json"),
                "list": load_json(self.capture.base / point.fork_point_id / "global-evidence.json"),
            }
            for point in points
        ]
        for path in sorted((self.capture.base / "failures").glob("*.json")):
            failure = load_json(path)
            if (failure.get("run_id"), failure.get("candidate_id")) == (run_id, candidate_id):
                results.append({"state": "unavailable", **failure})
        if not results:
            results.append({
                "state": "unavailable", "reason": "HistoricalRunHasNoCapturedForkPoint",
                "run_id": run_id, "candidate_id": candidate_id,
            })
        return results

    def read(self, experiment_id: str) -> ReplayExperiment:
        if not experiment_id.startswith("replay_") or len(experiment_id) != 39:
            raise ValueError("invalid ReplayExperiment identity")
        return ReplayExperiment.model_validate(load_json(self.base / experiment_id / "manifest.json"))

    def create(
        self, fork_point_id: str, targets: list[tuple[str, int]], *, request_id: str,
    ) -> ReplayExperiment:
        if not targets or len(targets) != len(set(targets)):
            raise ValueError("select a nonempty set of distinct Evidence targets")
        point = self.capture.read(fork_point_id)
        if point.state != "ready":
            raise RuntimeError(f"ForkPoint is {point.state}: {point.reason or 'not finalized'}")
        directory = self.capture.base / fork_point_id
        prefix = load_json(directory / "prefix.json")
        if sha256_text(canonical_json(prefix)) != point.native_prefix_digest:
            raise RuntimeError("NativePrefixDigestMismatch")
        visible = load_json(directory / "global-evidence.json")
        if sha256_text(canonical_json(visible)) != point.global_snapshot_id:
            raise RuntimeError("GlobalEvidenceSnapshotMismatch")
        selected: list[ReplayTarget] = []
        for candidate_id, iteration in targets:
            rows = [
                row for row in visible
                if row.get("candidate_id") == candidate_id and row.get("iteration") == iteration
            ]
            eligible = [
                item for item in point.targets
                if (item.candidate_id, item.iteration) == (candidate_id, iteration)
            ]
            if len(rows) != 1 or len(eligible) != 1:
                raise ValueError(f"Evidence target is not uniquely expandable: {candidate_id}:{iteration}")
            target = eligible[0]
            if rows[0].get("artifact_ref") != target.artifact_ref.model_dump(mode="json"):
                raise ValueError("Evidence artifact identity changed")
            detail = load_json(directory / "details" / f"{target.detail_snapshot_id}.json")
            if (
                sha256_text(canonical_json(detail)) != target.detail_snapshot_id
                or (detail.get("candidate_id"), detail.get("iteration")) != (candidate_id, iteration)
                or detail.get("commit") != rows[0].get("commit")
            ):
                raise ValueError("Evidence detail identity or content mismatch")
            selected.append(target)
        with exclusive_file_lock(self.base / "create.lock"):
            if self.capture.read(fork_point_id).state != "ready":
                raise RuntimeError("ForkPoint became unavailable during creation")
            existing = [
                self.read(path.parent.name)
                for path in self.base.glob("replay_*/manifest.json")
                if load_json(path).get("request_id") == request_id
            ]
            if existing:
                experiment = existing[0]
                if experiment.fork_point_id != fork_point_id or [b.target for b in experiment.branches] != selected:
                    raise ValueError("request_id already belongs to a different replay selection")
                return experiment
            experiment_id = f"replay_{uuid.uuid4().hex}"
            experiment = ReplayExperiment(
                experiment_id=experiment_id, fork_point_id=fork_point_id,
                request_id=request_id, created_at=datetime.now(timezone.utc).isoformat(),
                branches=[ReplayBranch(branch_id=f"branch_{uuid.uuid4().hex}", target=item)
                          for item in selected],
            )
            write_json(self.base / experiment_id / "manifest.json", experiment.model_dump(mode="json"))
        return experiment

    def _update_branch(self, experiment: ReplayExperiment, branch: ReplayBranch) -> ReplayExperiment:
        path = self.base / experiment.experiment_id / "manifest.json"
        with exclusive_file_lock(path.parent / "experiment.lock"):
            current = self.read(experiment.experiment_id)
            branches = [branch if item.branch_id == branch.branch_id else item for item in current.branches]
            states = {item.state for item in branches}
            state = (
                "completed" if states == {"completed"}
                else "failed" if states <= {"completed", "failed"}
                else "running"
            )
            updated = current.model_copy(update={"branches": branches, "state": state})
            write_json(path, updated.model_dump(mode="json"))
            return updated

    def _materialize(self, point: ReplayForkPoint, experiment: ReplayExperiment, branch: ReplayBranch) -> tuple[Path, Path]:
        source = self.capture.base / point.fork_point_id
        root = self.base / experiment.experiment_id / branch.branch_id / "runtime"
        if root.exists():
            raise RuntimeError("ReplayBranchAlreadyMaterialized")
        run = root / "runs" / point.run_id
        shutil.copytree(source / "run", run, symlinks=True)
        shutil.rmtree(run / "reference-workspaces", ignore_errors=True)
        shutil.copytree(source / "spec", root / "specs" / point.frozen_spec_id)
        if (source / "goal").is_dir():
            goal_id = load_json(source / "goal" / "goal.json")["goal_plus_id"]
            shutil.copytree(source / "goal", root / "goal-plus" / goal_id)
        captured_repositories = source / "candidate-repositories"
        repository_manifest_path = captured_repositories / "manifest.json"
        repository_manifest = (
            load_json(repository_manifest_path).get("candidates", {})
            if repository_manifest_path.is_file()
            else {}
        )
        source_repository = captured_repositories / f"{point.candidate_id}.git"
        if not source_repository.is_dir():
            source_repository = source / "repository.git"
        mirror = run / "workspace-repository.git"
        _run_git("clone", "--mirror", "--no-hardlinks", str(source_repository), str(mirror))
        queries = FileCandidateQueries(root)
        original = CandidateRecord.model_validate(load_json(
            source / "run" / "candidates" / point.candidate_id / "candidate.json"
        ))
        old_managed = original.task.workspace_managed_root or original.task.workspace
        managed = run / "workspace" / point.candidate_id
        if point.workspace_provider == "git_worktree":
            branch_name = original.task.workspace_branch
            if not branch_name:
                raise RuntimeError("ReplaySourceBranchUnavailable")
            _run_git("worktree", "add", str(managed), branch_name, cwd=mirror)
        else:
            _run_git("clone", "--no-hardlinks", str(mirror), str(managed))
            if _run_git("rev-parse", "HEAD", cwd=managed) != point.workspace_head:
                _run_git("checkout", "--detach", point.workspace_head, cwd=managed)
        if _run_git("rev-parse", "HEAD", cwd=managed) != point.workspace_head:
            raise RuntimeError("ReplayGitHeadMismatch")
        for item in managed.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
        shutil.copytree(source / "workspace-files", managed, dirs_exist_ok=True, symlinks=True)
        if workspace_digest(managed) != point.workspace_digest:
            raise RuntimeError("ReplayWorkspaceDigestMismatch")

        materialized_candidates = {point.candidate_id}
        for captured in sorted(captured_repositories.glob("*.git")):
            peer_candidate_id = captured.name.removesuffix(".git")
            if peer_candidate_id == point.candidate_id:
                continue
            peer = CandidateRecord.model_validate(load_json(
                source / "run" / "candidates" / peer_candidate_id / "candidate.json"
            ))
            peer_managed = run / "workspace" / peer_candidate_id
            _run_git("clone", "--no-hardlinks", str(captured), str(peer_managed))
            metadata = repository_manifest.get(peer_candidate_id, {})
            peer_head = metadata.get("workspace_head")
            if isinstance(peer_head, str):
                _run_git("checkout", "--detach", peer_head, cwd=peer_managed)
            if isinstance(peer_head, str) and _run_git("rev-parse", "HEAD", cwd=peer_managed) != peer_head:
                raise RuntimeError(f"ReplayPeerGitHeadMismatch:{peer_candidate_id}")
            materialized_candidates.add(peer_candidate_id)
        source_run = RunRecord.model_validate(load_json(source / "run" / "run.json"))
        frozen = queries.read_frozen_spec(point.frozen_spec_id)
        verifier_window_seconds = sum(
            float(command.timeout_seconds) + 2 * TERM_GRACE_SECONDS
            for command in frozen.spec.process_verifiers
        )
        baseline = root / "baseline-source"
        if point.workspace_provider == "git_worktree":
            baseline_ref = f"refs/heads/gp/{point.run_id}/baseline"
            if subprocess.run(["git", "-C", str(mirror), "show-ref", "--verify", "--quiet", baseline_ref]).returncode != 0:
                baseline_ref = source_run.source_artifact_ref.id if source_run.source_artifact_ref else point.workspace_head
            _run_git("worktree", "add", "--detach", str(baseline), baseline_ref, cwd=mirror)
        else:
            shutil.copytree(source / "baseline-source", baseline)
        source_path = Path(source_run.source_path)
        logical_baseline = baseline / source_run.source_subdir
        replacements = [
            (str(old_managed), str(managed)),
            (str(Path(point.source_root)), str(root)),
            (str(source_path), str(logical_baseline)),
        ]
        def is_mutable_state(path: Path) -> bool:
            parts = path.relative_to(run).parts
            if not parts or parts[0] in {
                "candidate-traces", "reference-workspaces", "workspace",
            }:
                return False
            if "logs" in parts or "verification" in parts:
                return False
            return parts[:2] != ("evidence-annotator", "artifact-views")

        state_paths = [path for path in run.rglob("*.json") if is_mutable_state(path)]
        state_paths.extend((root / "specs").rglob("*.json"))
        if (root / "goal-plus").exists():
            state_paths.extend((root / "goal-plus").rglob("*.json"))
        for path in state_paths:
            value = _rewrite(load_json(path), replacements)
            if path == run / "run.json":
                value["source_path"] = str(logical_baseline)
                value["source_repository"] = str(mirror)
            if path.name == "goal.json":
                value["outer_deadline_at"] = (
                    datetime.now(timezone.utc)
                    + timedelta(
                        seconds=point.remaining_runtime_seconds + verifier_window_seconds,
                    )
                ).isoformat()
            write_json(path, value)
        candidate_path = run / "candidates" / point.candidate_id / "candidate.json"
        candidate = load_json(candidate_path)
        candidate["task"]["workspace"] = str(managed / original.task.workspace.relative_to(old_managed))
        candidate["task"]["workspace_managed_root"] = str(managed)
        if point.workspace_provider == "git_worktree":
            candidate["task"]["workspace_provider_handle"]["repository"] = str(mirror)
        write_json(candidate_path, candidate)
        session_path = run / "agent_sessions" / f"{point.agent_session_id}.json"
        session = load_json(session_path)
        session["workspace"] = candidate["task"]["workspace"]
        session["launch"] = {**point.launch, "budget_control": {
            **(point.launch.get("budget_control") or {}),
            "max_runtime_seconds": point.remaining_runtime_seconds,
        }}
        write_json(session_path, session)
        queries.read_run(point.run_id)
        queries.read_frozen_spec(point.frozen_spec_id)
        for candidate_id in sorted(materialized_candidates):
            replay_candidate = queries.read_candidate(point.run_id, candidate_id)
            if not replay_candidate.task.workspace.is_dir():
                raise RuntimeError(f"ReplayCandidateWorkspaceMissing:{candidate_id}")
        queries.read_agent_session(point.agent_session_id, point.run_id)

        reference_state = FileReferenceState(root)
        reference_workspace = ProviderReferenceWorkspace()
        for reference in reference_state.read(point.run_id):
            if reference.state != "open":
                continue
            if reference.source_candidate_id not in materialized_candidates:
                raise RuntimeError(
                    f"ReplayReferenceRepositoryUnavailable:{reference.source_candidate_id}"
                )
            reference_candidate = reference_state.read_candidate(
                point.run_id, reference.source_candidate_id,
            )
            reference_root = reference_workspace.materialize(
                reference_candidate,
                reference.artifact_ref,
                reference_state.destination(
                    point.run_id, reference.requester_agent_session_id,
                ),
            )
            if reference.access.root is None or reference_root.resolve() != reference.access.root.resolve():
                raise RuntimeError("ReplayReferenceWorkspacePathMismatch")
        return root, Path(session["workspace"])

    def run(self, experiment_id: str) -> ReplayExperiment:
        experiment = self.read(experiment_id)
        if experiment.cleaned_at is not None:
            raise RuntimeError("ReplayExperimentAlreadyCleaned")
        point = self.capture.read(experiment.fork_point_id)
        prefix = load_json(self.capture.base / point.fork_point_id / "prefix.json")
        if point.state != "ready" or sha256_text(canonical_json(prefix)) != point.native_prefix_digest:
            raise RuntimeError("ReplayForkPointNoLongerVerified")
        for branch in experiment.branches:
            lock_path = self.base / experiment_id / branch.branch_id / "execution.lock"
            try:
                with exclusive_file_lock(lock_path, blocking=False):
                    self._run_claimed_branch(experiment_id, branch.branch_id, point, prefix)
            except BlockingIOError:
                continue
        return self.read(experiment_id)

    def _run_claimed_branch(
        self, experiment_id: str, branch_id: str, point: ReplayForkPoint,
        prefix: list[dict[str, Any]],
    ) -> None:
        experiment = self.read(experiment_id)
        branch = next(item for item in experiment.branches if item.branch_id == branch_id)
        if branch.state == "running":
            self._update_branch(experiment, branch.model_copy(update={
                "state": "failed", "error": "ReplayInterrupted: inspect retained branch runtime",
            }))
            return
        if branch.state != "created":
            return
        branch = branch.model_copy(update={"state": "running"})
        experiment = self._update_branch(experiment, branch)
        try:
            root, workspace = self._materialize(point, experiment, branch)
            detail = load_json(
                self.capture.base / point.fork_point_id / "details"
                / f"{branch.target.detail_snapshot_id}.json"
            )
            if sha256_text(canonical_json(detail)) != branch.target.detail_snapshot_id:
                raise RuntimeError("ReplayDetailDigestMismatch")
            thread_id = _fork_native(point, root, workspace, prefix)
            session_path = root / "runs" / point.run_id / "agent_sessions" / f"{point.agent_session_id}.json"
            session = load_json(session_path)
            session["session_handle"]["external_id"] = thread_id
            session["session_handle"]["metadata"] = {"transport": "app-server"}
            write_json(session_path, session)
            branch = branch.model_copy(update={
                "native_thread_id": thread_id,
                "result": {
                    "native_prefix_digest": point.native_prefix_digest,
                    "workspace_digest": point.workspace_digest,
                },
            })
            experiment = self._update_branch(experiment, branch)
            bridge = McpSessionToolBridge(
                root, caller_role="candidate", caller_agent_session_id=point.agent_session_id,
            )
            native = CodexSession(
                _native_input(root, workspace, point, thread_id), _registration(root), timeout=60,
                tool_bridge=bridge,
            )
            try:
                prompt = (
                    "[Goal Plus Evidence replay: host prefetch; this is not a candidate tool call]\n"
                    f"Selected Evidence: {branch.target.candidate_id}:{branch.target.iteration}\n"
                    f"Artifact: {branch.target.artifact_ref.model_dump_json()}\n"
                    f"Replay workspace: {workspace}\n"
                    f"Detail snapshot SHA-256: {branch.target.detail_snapshot_id}\n"
                    f"Detail: {canonical_json(detail)}\n"
                    "Continue the assigned task autonomously. Use the normal verifier for any changes."
                )
                branch = branch.model_copy(update={
                    "result": {
                        **branch.result,
                        "prefetch": {
                            "candidate_id": branch.target.candidate_id,
                            "iteration": branch.target.iteration,
                            "artifact_ref": branch.target.artifact_ref.model_dump(mode="json"),
                            "detail_snapshot_id": branch.target.detail_snapshot_id,
                            "prompt_digest": sha256_text(prompt),
                            "attempted_at": datetime.now(timezone.utc).isoformat(),
                        },
                    },
                })
                experiment = self._update_branch(experiment, branch)
                result = native.run(prompt, timeout=point.remaining_runtime_seconds)
            finally:
                native.close()
            runtime = FileSearchRuntime(root)
            runtime.candidate_sessions.record_native_invocation(point.agent_session_id, result)
            review = CandidateDelivery(runtime.queries, candidate_host_queries(root)).review(
                point.run_id, point.candidate_id,
            )
            if review.action == "verify":
                runtime.run_verifier(
                    point.run_id, point.candidate_id,
                    agent_session_id=point.agent_session_id,
                    hypothesis="Replay host verification of returned candidate changes",
                )
            candidate = FileCandidateQueries(root).read_candidate(point.run_id, point.candidate_id)
            new_iterations = candidate.iterations[point.source_iteration_count:]
            difference = _run_git("diff", "--binary", point.workspace_head, cwd=workspace)
            diff_path = root.parent / "code-diff.patch"
            diff_path.write_text(difference + ("\n" if difference else ""), encoding="utf-8")
            branch = branch.model_copy(update={
                "state": "completed" if result.state == "completed" else "failed",
                "result": {
                    **branch.result,
                    "native_turn_id": result.invocation_id,
                    "native_state": result.state,
                    "prefetch_detail_snapshot_id": branch.target.detail_snapshot_id,
                    "review_action": review.action,
                    "iterations": [item.model_dump(mode="json") for item in new_iterations],
                    "code_diff_path": str(diff_path),
                    "usage": result.usage,
                },
                "error": result.error,
            })
        except Exception as exc:
            branch = branch.model_copy(update={
                "state": "failed", "error": f"{type(exc).__name__}: {exc}",
            })
        self._update_branch(experiment, branch)

    def report(self, experiment_id: str) -> dict[str, Any]:
        experiment = self.read(experiment_id)
        point = self.capture.read(experiment.fork_point_id)
        frozen = load_json(self.capture.base / point.fork_point_id / "spec" / "frozen_spec.json")
        direction = frozen["spec"]["metric_direction"]
        try:
            original = FileCandidateQueries(point.source_root).read_candidate(
                point.run_id, point.candidate_id,
            )
            observed = original.iterations[point.source_iteration_count:]
            source_observational: dict[str, Any] = {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "verifier_attempts_after_fork": len(observed),
                "iterations": [item.model_dump(mode="json") for item in observed],
            }
        except (OSError, ValueError):
            source_observational = {"state": "unavailable"}
        summaries = []
        for branch in experiment.branches:
            attempts = branch.result.get("iterations") or []
            passing = [item for item in attempts if item.get("process_passed") is True]
            scored = [
                item for item in passing
                if isinstance(item.get("score"), (int, float))
            ]
            best = (
                (min if direction == "minimize" else max)(
                    scored, key=lambda item: item["score"],
                )
                if scored else None
            )
            summaries.append({
                "branch_id": branch.branch_id,
                "target": branch.target.model_dump(mode="json"),
                "state": branch.state,
                "delivered_detail_snapshot_id": branch.result.get("prefetch_detail_snapshot_id"),
                "verifier_attempts": len(attempts),
                "first_valid_iteration": passing[0]["iteration"] if passing else None,
                "best_iteration": best["iteration"] if best is not None else None,
                "best_score": best["score"] if best is not None else None,
                "final_score": attempts[-1].get("score") if attempts else None,
                "code_diff_path": branch.result.get("code_diff_path"),
                "usage": branch.result.get("usage"),
                "error": branch.error,
            })
        return {
            "experiment_id": experiment_id,
            "fork_point_id": point.fork_point_id,
            "source_run_id": point.run_id,
            "source_candidate_id": point.candidate_id,
            "source_list_snapshot_id": point.global_snapshot_id,
            "metric_direction": direction,
            "native_prefix_digest": point.native_prefix_digest,
            "workspace_digest": point.workspace_digest,
            "remaining_runtime_seconds_per_branch": point.remaining_runtime_seconds,
            "branches": [branch.model_dump(mode="json") for branch in experiment.branches],
            "comparison": summaries,
            "source_observational": source_observational,
        }

    def cleanup(self, experiment_id: str, *, confirmed_stopped: bool = False) -> ReplayExperiment:
        path = self.base / experiment_id / "manifest.json"
        with exclusive_file_lock(path.parent / "experiment.lock"):
            experiment = self.read(experiment_id)
            if experiment.cleaned_at is not None:
                return experiment
            if any(branch.state not in {"completed", "failed"} for branch in experiment.branches):
                raise RuntimeError("ReplayExperimentStillRunning")
            if not confirmed_stopped and any(
                (branch.error or "").startswith("ReplayInterrupted:")
                for branch in experiment.branches
            ):
                raise RuntimeError("ReplayInterruptedBranchRequiresManualProcessInspection")
            for branch in experiment.branches:
                runtime = path.parent / branch.branch_id / "runtime"
                if runtime.exists():
                    shutil.rmtree(runtime)
            experiment = experiment.model_copy(update={
                "cleaned_at": datetime.now(timezone.utc).isoformat(),
            })
            write_json(path, experiment.model_dump(mode="json"))
            return experiment

    def release(self, fork_point_id: str) -> ReplayForkPoint:
        with exclusive_file_lock(self.base / "create.lock"):
            point = self.capture.read(fork_point_id)
            if point.state == "unavailable" and point.reason == "ReleasedByOperator":
                return point
            experiments = [
                self.read(path.parent.name)
                for path in self.base.glob("replay_*/manifest.json")
                if load_json(path).get("fork_point_id") == fork_point_id
            ]
            if any(item.cleaned_at is None for item in experiments):
                raise RuntimeError("ReplayForkPointHasUncleanedExperiments")
            directory = self.capture.base / fork_point_id
            with exclusive_file_lock(directory / "fork.lock"):
                point = self.capture.read(fork_point_id)
                point = point.model_copy(update={
                    "state": "unavailable", "reason": "ReleasedByOperator",
                })
                write_json(directory / "manifest.json", point.model_dump(mode="json"))
                for name in (
                    "run", "goal", "workspace-files", "repository.git",
                    "candidate-repositories",
                ):
                    retained = directory / name
                    if retained.exists():
                        shutil.rmtree(retained)
                return point
