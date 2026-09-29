"""Deterministic local verifier execution and workspace integrity checks."""

from __future__ import annotations
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal
from goal_plus.domain.worker_policy import process_environment_for_index
from goal_plus.infrastructure.artifacts.git import git_output
from goal_plus.infrastructure.process_output import capture_process
from goal_plus.infrastructure.process_output import TERM_GRACE_SECONDS as VERIFIER_TERM_GRACE_SECONDS
from goal_plus.domain.artifacts import ArtifactRef, WorkspaceProvider
from goal_plus.domain.evidence import ScoreReport, VerifierResult
from goal_plus.domain.search import (
    BootstrapVerification,
    BootstrapVerifierResult,
    DevicePoolInventory,
    DevicePoolSpec,
    FeedbackPolicy,
    FrozenSpec,
    SearchSpec,
    VerifierCommand,
    VerifierRole,
    CandidateTask,
)
from goal_plus.domain.serialization import sha256_text
from goal_plus.infrastructure.clock import utc_timestamp as utc_timestamp
from goal_plus.infrastructure.state.files import sha256_file
from goal_plus.infrastructure.state.resource_locks import verifier_resource_lock
from goal_plus.infrastructure.verifiers.output import bounded_verifier_log
from goal_plus.infrastructure.verifiers.command import verifier_argv
from goal_plus.ports.state import CandidateRecord, RunRecord
from goal_plus.workspaces import (
    IGNORED_NAMES,
    IGNORED_SUFFIXES,
    copy_source_tree,
    initialize_workspace_git_baseline,
    GitReferences,
)

VERIFIER_PHASE_ENV = "GOAL_PLUS_VERIFIER_PHASE"


VERIFIER_DIAGNOSTICS_ENV = "GOAL_PLUS_VERIFIER_DIAGNOSTICS_DIR"


VERIFIER_RESOURCE_ENV = "GOAL_PLUS_VERIFIER_RESOURCE"


MAX_VERIFIER_FEEDBACK_CHARS = 4_000


def safe_verifier_name(value: str) -> str:
    readable = "".join(
        character if character.isalnum() or character in {".", "_", "-"} else "_"
        for character in value
    ).strip("._-")
    return f"{readable or 'verifier'}-{sha256_text(value)[:8]}"


class LocalVerifiers:
    termination_grace_seconds = VERIFIER_TERM_GRACE_SECONDS

    def __init__(self, root_dir: Path | str) -> None:
        self.runs_dir = Path(root_dir) / "runs"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    @contextmanager
    def workspace(self, record: CandidateRecord, artifact: ArtifactRef) -> Iterator[CandidateTask]:
        task = record.task
        references = GitReferences(task.workspace_provider)
        parent = self.run_dir(task.run_id) / "candidates" / record.candidate_id / "verification"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            destination = Path(directory) / record.candidate_id
            logical = references.materialize_reference(task.workspace, artifact, destination)
            try:
                yield task.model_copy(update={"workspace": logical, "workspace_managed_root": destination})
            finally:
                # Keep detached settlement/ledger commits reachable after removing the checkout.
                head = self.git_head(logical)
                if head is not None:
                    git_output(task.workspace, ["git", "update-ref", f"refs/goal-plus/verified/{task.run_id}/{task.candidate_id}", head])
                references.remove_reference(task.workspace, artifact, destination)

    def execute_verifier_process(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
        pass_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        if not text or not capture_output:
            raise ValueError("verifier processes require text capture")

        process = subprocess.Popen(
            verifier_argv(command),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
        )
        completed = capture_process(process, timeout=timeout)
        if check:
            completed.check_returncode()
        return completed

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
    ) -> BootstrapVerification:
        started_at = utc_timestamp()
        log_dir.mkdir(parents=True, exist_ok=True)
        results: list[BootstrapVerifierResult] = []
        process_environment = process_environment_for_index(
            spec.device_pools,
            device_inventory,
            0,
        )

        initial_hash_failures = self.bootstrap_hash_failures(
            workspace,
            verifier_hashes,
        )
        if initial_hash_failures:
            results.append(
                BootstrapVerifierResult(
                    name="frozen_hash_check",
                    role=VerifierRole.ANTI_CHEAT_GATE,
                    passed=False,
                    score=0.0,
                    metrics={"hash_failures": initial_hash_failures},
                    failure_class="FrozenVerifierModified",
                )
            )
        else:
            for index, command in enumerate(spec.process_verifiers, start=1):
                result = self.run_bootstrap_command(
                    spec=spec,
                    workspace=workspace,
                    command=command,
                    phase=phase,
                    log_path=(
                        log_dir / f"{index:02d}-{safe_verifier_name(command.name)}.log"
                    ),
                    verifier_hashes=verifier_hashes,
                    device_inventory=device_inventory,
                    process_environment=process_environment,
                )
                results.append(result)

            final_hash_failures = self.bootstrap_hash_failures(
                workspace,
                verifier_hashes,
            )
            if final_hash_failures:
                results.append(
                    BootstrapVerifierResult(
                        name="frozen_hash_check_after_verifier",
                        role=VerifierRole.ANTI_CHEAT_GATE,
                        passed=False,
                        score=0.0,
                        metrics={"hash_failures": final_hash_failures},
                        failure_class="FrozenVerifierModified",
                    )
                )

        hard_failed = any(
            not result.passed
            and result.role
            in {
                VerifierRole.VALIDITY_GATE,
                VerifierRole.PROCESS_GATE,
                VerifierRole.RANKING_SIGNAL,
                VerifierRole.ANTI_CHEAT_GATE,
            }
            for result in results
        )
        passed = not hard_failed and all(
            result.passed or result.role == VerifierRole.DIAGNOSTIC_SIGNAL
            for result in results
        )
        aggregate_score = self.aggregate_score(spec.metric_name, results)
        return BootstrapVerification(
            phase=phase,
            workspace_provider=workspace_provider,
            passed=passed,
            aggregate_score=aggregate_score,
            verifier_results=results,
            source_artifact_ref=source_artifact_ref,
            candidate_id=candidate_id,
            started_at=started_at,
            completed_at=utc_timestamp(),
        )

    def run_bootstrap_command(
        self,
        *,
        spec: SearchSpec,
        workspace: Path,
        command: VerifierCommand,
        phase: Literal["origin", "workspace_relocation"],
        log_path: Path,
        verifier_hashes: dict[str, str],
        device_inventory: dict[str, DevicePoolInventory],
        process_environment: dict[str, str],
    ) -> BootstrapVerifierResult:
        cwd = (workspace / command.cwd).resolve()
        try:
            cwd.relative_to(workspace.resolve())
        except ValueError:
            return BootstrapVerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics={"cwd": str(cwd)},
                failure_class="VerifierCwdEscapedWorkspace",
            )
        if not cwd.is_dir():
            return BootstrapVerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics={"cwd": str(cwd)},
                failure_class="VerifierCwdMissing",
            )
        if command.command[0] == "goal-plus-internal":
            known = command.command[1:] == ["check-frozen-hashes"]
            failures = (
                self.bootstrap_hash_failures(workspace, verifier_hashes)
                if known
                else {"command": {"expected": "check-frozen-hashes", "actual": None}}
            )
            return BootstrapVerifierResult(
                name=command.name,
                role=command.role,
                passed=known and not failures,
                score=None,
                metrics={"hash_failures": failures},
                failure_class=(
                    None
                    if known and not failures
                    else "FrozenVerifierModified"
                    if known
                    else "UnknownInternalCommand"
                ),
            )

        diagnostics_dir = log_path.parent / "diagnostics" / log_path.stem
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        start = time.perf_counter()
        resource = self.verifier_resource(
            command.resource_lock,
            spec.device_pools,
            device_inventory,
            process_environment,
        )
        try:
            with verifier_resource_lock(resource):
                with tempfile.TemporaryDirectory(
                    prefix="goal-plus-verifier-command-"
                ) as verifier_tmp:
                    completed = self.execute_verifier_process(
                        command.command,
                        cwd=cwd,
                        env=self.verifier_environment(
                            cwd,
                            Path(verifier_tmp),
                            phase=phase,
                            diagnostics_dir=diagnostics_dir,
                            resource=resource,
                            process_environment=process_environment,
                        ),
                        text=True,
                        capture_output=True,
                        timeout=command.timeout_seconds,
                        check=False,
                    )
            metrics = self.parse_metrics(completed.stdout)
            score = self.score_from_metrics(spec.metric_name, metrics)
            metrics["returncode"] = completed.returncode
            metrics["elapsed_seconds"] = time.perf_counter() - start
            metrics.update(self.verifier_diagnostics(diagnostics_dir))
            failure_class = self.command_failure(
                command.role, completed.returncode, metrics, score,
            )
            log_path.write_text(
                bounded_verifier_log(
                    "\n".join(
                        [
                            f"$ {' '.join(command.command)}",
                            f"cwd: {cwd}",
                            f"returncode: {completed.returncode}",
                            "",
                            "## stdout",
                            completed.stdout,
                            "## stderr",
                            completed.stderr,
                        ]
                    )
                ),
                encoding="utf-8",
            )
            return BootstrapVerifierResult(
                name=command.name,
                role=command.role,
                passed=failure_class is None,
                score=score,
                metrics=metrics,
                log_path=log_path,
                failure_class=failure_class,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = self.coerce_verifier_output(exc.stdout)
            stderr = self.coerce_verifier_output(exc.stderr)
            log_path.write_text(
                bounded_verifier_log(
                    "\n".join(
                        [
                            f"$ {' '.join(command.command)}",
                            f"cwd: {cwd}",
                            f"timeout_seconds: {command.timeout_seconds}",
                            "",
                            "## stdout",
                            stdout,
                            "## stderr",
                            stderr,
                        ]
                    )
                ),
                encoding="utf-8",
            )
            return BootstrapVerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics={"timeout_seconds": command.timeout_seconds},
                log_path=log_path,
                failure_class="Timeout",
            )
        except OSError as exc:
            log_path.write_text(bounded_verifier_log(str(exc)), encoding="utf-8")
            return BootstrapVerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics={"error": str(exc)},
                log_path=log_path,
                failure_class="VerifierStartFailed",
            )

    @staticmethod
    def bootstrap_hash_failures(
        workspace: Path,
        verifier_hashes: dict[str, str],
    ) -> dict[str, dict[str, str | None]]:
        failures: dict[str, dict[str, str | None]] = {}
        for relative_path, expected_hash in verifier_hashes.items():
            path = workspace / relative_path
            actual_hash = sha256_file(path) if path.is_file() else None
            if actual_hash != expected_hash:
                failures[relative_path] = {
                    "expected": expected_hash,
                    "actual": actual_hash,
                }
        return failures

    def preflight_process_verifiers(
        self,
        spec: SearchSpec,
        device_inventory: dict[str, DevicePoolInventory],
    ) -> BootstrapVerification:
        source_root = Path(spec.source_path).resolve()
        source_workspace = source_root if source_root.is_dir() else source_root.parent
        with tempfile.TemporaryDirectory(prefix="goal-plus-verifier-preflight-") as directory:
            workspace = Path(directory) / "workspace"
            copy_source_tree(source_workspace, workspace)
            initialize_workspace_git_baseline(workspace)
            before = self.hash_verifier_workspace(workspace)
            baseline = self.run_bootstrap_verifiers(
                spec=spec, workspace=workspace, workspace_provider=spec.workspace.provider,
                phase="origin", verifier_hashes={},
                log_dir=self.runs_dir.parent / "bootstrap" / f"origin-{uuid.uuid4().hex[:12]}",
                source_artifact_ref=None, device_inventory=device_inventory,
            )
            side_effects = self.hash_changes(before, self.hash_verifier_workspace(workspace))
            if side_effects:
                raise ValueError(
                    f"VerifierWorkspaceSideEffect: process suite changed disposable workspace: {side_effects}. "
                    "Use GOAL_PLUS_VERIFIER_TMPDIR/TMPDIR for per-invocation outputs; "
                    "verifiers may run concurrently."
                )
            return baseline

    def verifier_environment(
        self,
        cwd: Path,
        temp_dir: Path,
        *,
        phase: Literal[
            "origin",
            "workspace_relocation",
            "candidate",
            "promotion",
        ],
        diagnostics_dir: Path | None = None,
        resource: str | None = None,
        process_environment: dict[str, str] | None = None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env.update(process_environment or {})
        env.pop("PYTHONHOME", None)
        env["PYTHONPATH"] = str(cwd)
        env["PYTHONNOUSERSITE"] = "1"
        for name in ("TMPDIR", "TMP", "TEMP", "GOAL_PLUS_VERIFIER_TMPDIR"):
            env[name] = str(temp_dir)
        # Keep verifier imports deterministic when a candidate rewrites a Python
        # module with the same size inside the filesystem timestamp granularity.
        # A per-invocation cache root prevents stale workspace bytecode from
        # shadowing the current candidate source and remains concurrency-safe.
        env["PYTHONPYCACHEPREFIX"] = str(temp_dir / "pycache")
        env[VERIFIER_PHASE_ENV] = phase
        if diagnostics_dir is not None:
            env[VERIFIER_DIAGNOSTICS_ENV] = str(diagnostics_dir)
        else:
            env.pop(VERIFIER_DIAGNOSTICS_ENV, None)
        if resource is not None:
            env[VERIFIER_RESOURCE_ENV] = resource
        else:
            env.pop(VERIFIER_RESOURCE_ENV, None)
        return env

    def hash_changes(
        self,
        before: dict[str, str],
        after: dict[str, str],
    ) -> list[str]:
        return [
            path
            for path in sorted(set(before) | set(after))
            if before.get(path) != after.get(path)
        ]

    def hash_verifier_workspace(self, root: Path) -> dict[str, str]:
        hashes: dict[str, str] = {}
        ignored_names = IGNORED_NAMES - {".tmp"}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel_path = path.relative_to(root)
            if any(part in ignored_names for part in rel_path.parts):
                continue
            if path.suffix in IGNORED_SUFFIXES:
                continue
            hashes[rel_path.as_posix()] = sha256_file(path)
        return hashes

    @staticmethod
    def verifier_resource(
        resource: str | None,
        device_pools: list[DevicePoolSpec],
        device_inventory: dict[str, DevicePoolInventory],
        process_environment: dict[str, str] | None = None,
    ) -> str | None:
        if not resource:
            return resource
        pool_id, separator, index = resource.rpartition(":")
        pool = next(
            (item for item in device_pools if item.pool_id == pool_id),
            None,
        )
        if not separator or pool is None:
            return resource
        logical_index = int(index)
        configured = device_inventory.get(pool.pool_id)
        if configured is None or logical_index != 0:
            return resource
        physical_device = (process_environment or {}).get(configured.visibility_env)
        if not physical_device:
            return resource
        return f"{pool.pool_id}:{physical_device}"

    def run_commands(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        commands: list[VerifierCommand],
        scope: str,
        *, execution_lock: int | None = None,
    ) -> ScoreReport:
        verifier_phase: Literal["candidate", "promotion"] = (
            "candidate" if scope == "process" else "promotion"
        )
        results: list[VerifierResult] = []
        for command in commands:
            result = self.run_command(
                run,
                frozen,
                record,
                command,
                verifier_phase,
                execution_lock=execution_lock,
            )
            results.append(result)
            if result.failure_class == "VerifierWorkspaceSideEffect":
                break
        hard_failed = any(
            not result.passed
            and result.role
            in {
                VerifierRole.VALIDITY_GATE,
                VerifierRole.PROCESS_GATE,
                VerifierRole.PROMOTION_GATE,
                VerifierRole.ANTI_CHEAT_GATE,
            }
            for result in results
        )
        process_passed = not hard_failed and all(
            result.passed or result.role == VerifierRole.DIAGNOSTIC_SIGNAL
            for result in results
        )
        score = self.aggregate_score(frozen.spec.metric_name, results)
        if not process_passed:
            score = 0.0

        return ScoreReport(
            run_id=run.run_id,
            candidate_id=record.candidate_id,
            parent_id=record.task.parent_id,
            validity_passed=process_passed,
            process_passed=process_passed,
            promotion_passed=process_passed if scope == "promotion" else None,
            aggregate_score=score,
            verifier_results=results,
            touched_denied_files=record.touched_denied_files,
            changed_outside_allowed=record.changed_outside_allowed,
            hardcoding_suspected=False,
        )

    def run_command(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        command: VerifierCommand,
        verifier_phase: Literal["candidate", "promotion"],
        *, execution_lock: int | None = None,
    ) -> VerifierResult:
        if command.command[0] == "goal-plus-internal":
            start = time.perf_counter()
            result = self.run_internal_command(frozen, record, command)
            result.metrics["elapsed_seconds"] = max(0.0, time.perf_counter() - start)
            return result

        log_scope = "process" if verifier_phase == "candidate" else "promotion"
        logs_dir = (
            self.run_dir(run.run_id)
            / "candidates"
            / record.candidate_id
            / "logs"
            / log_scope
        )
        logs_dir.mkdir(parents=True, exist_ok=True)
        command_log_name = safe_verifier_name(command.name)
        iteration_number = len(record.iterations) + 1
        log_path = logs_dir / (
            f"iteration-{iteration_number:04d}-{command_log_name}-"
            f"{uuid.uuid4().hex[:8]}.log"
        )
        diagnostics_dir = (
            logs_dir / "diagnostics" / f"{command_log_name}-{uuid.uuid4().hex[:12]}"
        )
        diagnostics_dir.mkdir(parents=True, exist_ok=False)
        cwd = (record.task.workspace / command.cwd).resolve()
        workspace_before = self.hash_verifier_workspace(record.task.workspace)
        git_head_before = self.git_head(record.task.workspace)
        resource = self.verifier_resource(
            command.resource_lock,
            frozen.spec.device_pools,
            frozen.device_inventory,
            record.task.process_environment,
        )
        start = time.perf_counter()
        try:
            with verifier_resource_lock(resource) as resource_lease:
                with tempfile.TemporaryDirectory(
                    prefix="goal-plus-verifier-command-"
                ) as verifier_tmp:
                    completed = self.execute_verifier_process(
                        command.command,
                        cwd=cwd,
                        env=self.verifier_environment(
                            cwd,
                            Path(verifier_tmp),
                            phase=verifier_phase,
                            diagnostics_dir=diagnostics_dir,
                            resource=resource,
                            process_environment=record.task.process_environment,
                        ),
                        text=True,
                        capture_output=True,
                        timeout=command.timeout_seconds,
                        check=False,
                        pass_fds=tuple(fd for fd in (execution_lock, resource_lease) if fd is not None),
                    )
            elapsed = time.perf_counter() - start
            if completed.returncode < 0:
                log_path.write_text(
                    bounded_verifier_log(
                        f"returncode: {completed.returncode}\n\n"
                        f"## stdout\n{completed.stdout}\n## stderr\n{completed.stderr}"
                    ),
                    encoding="utf-8",
                )
                raise RuntimeError(
                    f"VerifierInterrupted: {command.name} terminated by signal "
                    f"{-completed.returncode}; see {log_path}"
                )
            metrics = self.parse_metrics(completed.stdout)
            score = self.score_from_metrics(frozen.spec.metric_name, metrics)
            metrics["returncode"] = completed.returncode
            metrics["elapsed_seconds"] = elapsed
            metrics.update(self.verifier_diagnostics(diagnostics_dir))
            side_effects = self.hash_changes(
                workspace_before,
                self.hash_verifier_workspace(record.task.workspace),
            )
            workspace_failure = bool(side_effects)
            if side_effects:
                cleanup_failures = self.restore_verifier_workspace(
                    record.task.workspace,
                    workspace_before,
                    side_effects,
                    git_head_before,
                )
                if self.disposable_outputs_cleaned(
                    verifier_phase, workspace_before, side_effects, cleanup_failures,
                ):
                    metrics["verifier_workspace_outputs_cleaned"] = side_effects
                    workspace_failure = False
            if workspace_failure:
                metrics.update(
                    {
                        "verifier_workspace_side_effects": side_effects,
                        "cleanup_failures": cleanup_failures,
                        "infrastructure_failure": True,
                        "candidate_action": "stop_and_report",
                    }
                )
                self.add_visible_verifier_feedback(
                    command,
                    metrics,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
                log_path.write_text(
                    bounded_verifier_log(
                        "\n".join(
                            [
                                f"$ {' '.join(command.command)}",
                                f"cwd: {cwd}",
                                f"returncode: {completed.returncode}",
                                f"verifier_workspace_side_effects: {side_effects}",
                                f"cleanup_failures: {cleanup_failures}",
                                "",
                                "## stdout",
                                completed.stdout,
                                "## stderr",
                                completed.stderr,
                            ]
                        )
                    ),
                    encoding="utf-8",
                )
                return VerifierResult(
                    name=command.name,
                    role=command.role,
                    passed=False,
                    score=0.0,
                    metrics=metrics,
                    log_path=log_path,
                    failure_class="VerifierWorkspaceSideEffect",
                )
            failure_class = self.command_failure(
                command.role, completed.returncode, metrics, score,
            )
            if failure_class == "MissingNumericMetric":
                metrics["expected_metric_name"] = frozen.spec.metric_name
            if failure_class is not None:
                self.add_visible_verifier_feedback(
                    command,
                    metrics,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
            log_path.write_text(
                bounded_verifier_log(
                    "\n".join(
                        [
                            f"$ {' '.join(command.command)}",
                            f"cwd: {cwd}",
                            f"returncode: {completed.returncode}",
                            "",
                            "## stdout",
                            completed.stdout,
                            "## stderr",
                            completed.stderr,
                        ]
                    )
                ),
                encoding="utf-8",
            )
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=failure_class is None,
                score=score,
                metrics=metrics,
                log_path=log_path,
                failure_class=failure_class,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = max(0.0, time.perf_counter() - start)
            side_effects = self.hash_changes(
                workspace_before,
                self.hash_verifier_workspace(record.task.workspace),
            )
            cleanup_failures = (
                self.restore_verifier_workspace(
                    record.task.workspace,
                    workspace_before,
                    side_effects,
                    git_head_before,
                )
                if side_effects
                else []
            )
            outputs_cleaned = self.disposable_outputs_cleaned(
                verifier_phase, workspace_before, side_effects, cleanup_failures,
            )
            workspace_failure = bool(side_effects) and not outputs_cleaned
            stdout = self.coerce_verifier_output(exc.stdout)
            stderr = self.coerce_verifier_output(exc.stderr)
            log_path.write_text(
                bounded_verifier_log(
                    "\n".join(
                        [
                            f"$ {' '.join(command.command)}",
                            f"cwd: {cwd}",
                            f"timeout_seconds: {command.timeout_seconds}",
                            "",
                            "## stdout",
                            stdout,
                            "## stderr",
                            stderr,
                        ]
                    )
                ),
                encoding="utf-8",
            )
            metrics: dict[str, Any] = {
                "timeout_seconds": command.timeout_seconds,
                "elapsed_seconds": elapsed,
                "verifier_workspace_side_effects": side_effects if workspace_failure else [],
                "cleanup_failures": cleanup_failures,
                "infrastructure_failure": workspace_failure,
                "candidate_action": (
                    "stop_and_report" if workspace_failure else "inspect_timeout"
                ),
            }
            if outputs_cleaned:
                metrics["verifier_workspace_outputs_cleaned"] = side_effects
            metrics.update(self.verifier_diagnostics(diagnostics_dir))
            self.add_visible_verifier_feedback(
                command,
                metrics,
                stdout=stdout,
                stderr=stderr,
            )
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics=metrics,
                log_path=log_path,
                failure_class=(
                    "VerifierWorkspaceSideEffect" if workspace_failure else "Timeout"
                ),
            )
        except OSError as exc:
            elapsed = max(0.0, time.perf_counter() - start)
            log_path.write_text(bounded_verifier_log(str(exc)), encoding="utf-8")
            metrics: dict[str, Any] = {
                "error": str(exc),
                "elapsed_seconds": elapsed,
            }
            metrics.update(self.verifier_diagnostics(diagnostics_dir))
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics=metrics,
                log_path=log_path,
                failure_class="VerifierStartFailed",
            )

    def verifier_diagnostics(self, diagnostics_dir: Path) -> dict[str, Any]:
        files = sorted(
            path.relative_to(diagnostics_dir).as_posix()
            for path in diagnostics_dir.rglob("*")
            if path.is_file()
        )
        if not files:
            shutil.rmtree(diagnostics_dir, ignore_errors=True)
            return {}
        return {
            "diagnostics_dir": str(diagnostics_dir),
            "diagnostic_files": files,
        }

    @staticmethod
    def coerce_verifier_output(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    def add_visible_verifier_feedback(
        self,
        command: VerifierCommand,
        metrics: dict[str, Any],
        *,
        stdout: str | bytes | None,
        stderr: str | bytes | None,
    ) -> None:
        if command.feedback_policy != FeedbackPolicy.VISIBLE_TO_WORKERS:
            return
        for key, value in (("stdout_tail", stdout), ("stderr_tail", stderr)):
            output = self.coerce_verifier_output(value).strip()
            if output:
                metrics[key] = output[-MAX_VERIFIER_FEEDBACK_CHARS:]

    def restore_verifier_workspace(
        self,
        workspace: Path,
        before: dict[str, str],
        side_effects: list[str],
        git_head_before: str | None,
    ) -> list[str]:
        cleanup_failures: list[str] = []
        if git_head_before is not None:
            try:
                git_output(
                    workspace,
                    ["git", "reset", "--hard", git_head_before],
                )
            except (FileNotFoundError, subprocess.CalledProcessError):
                cleanup_failures.extend(path for path in side_effects if path in before)
        else:
            cleanup_failures.extend(path for path in side_effects if path in before)

        for rel_path in side_effects:
            if rel_path in before:
                continue
            target = workspace / rel_path
            try:
                if target.is_file() or target.is_symlink():
                    target.unlink()
                parent = target.parent
                while parent != workspace:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            except OSError:
                cleanup_failures.append(rel_path)

        remaining = self.hash_changes(
            before,
            self.hash_verifier_workspace(workspace),
        )
        cleanup_failures.extend(
            path for path in remaining if path not in cleanup_failures
        )
        return cleanup_failures

    @staticmethod
    def disposable_outputs_cleaned(
        verifier_phase: Literal["candidate", "promotion"],
        before: dict[str, str],
        changes: list[str],
        cleanup_failures: list[str],
    ) -> bool:
        return (
            verifier_phase == "candidate"
            and bool(changes)
            and not cleanup_failures
            and all(path not in before for path in changes)
        )

    def run_internal_command(
        self,
        frozen: FrozenSpec,
        record: CandidateRecord,
        command: VerifierCommand,
    ) -> VerifierResult:
        if len(command.command) < 2 or command.command[1] != "check-frozen-hashes":
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics={"error": "unknown internal command"},
                failure_class="UnknownInternalCommand",
            )
        failures = self.frozen_hash_failures(frozen, record.task.workspace)
        return VerifierResult(
            name=command.name,
            role=command.role,
            passed=not failures,
            score=None,
            metrics={"hash_failures": failures},
            failure_class=None if not failures else "FrozenVerifierModified",
        )

    def parse_metrics(self, stdout: str) -> dict[str, Any]:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    def score_from_metrics(
        self, metric_name: str, metrics: dict[str, Any]
    ) -> float | None:
        value = metrics.get(metric_name)
        if isinstance(value, int | float) and not isinstance(value, bool):
            score = float(value)
            if math.isfinite(score):
                return score
        return None

    def command_failure(
        self, role: VerifierRole, returncode: int | None,
        metrics: dict[str, Any], score: float | None,
    ) -> str | None:
        outcome = metrics.get("outcome")
        if "outcome" in metrics and outcome not in ("valid", "invalid", "error"):
            return "InvalidVerifierOutcome"
        if outcome == "invalid" and returncode in (0, 1):
            if metrics.get("valid") is True:
                return "InvalidVerifierOutcome"
            if role == VerifierRole.RANKING_SIGNAL and score is None:
                return "MissingNumericMetric"
            return "CandidateRejected"
        if returncode != 0 or metrics.get("error") is not None or outcome == "error":
            return "VerifierCommandFailed"
        if metrics.get("valid") is False:
            return "InvalidVerifierOutcome"
        if role == VerifierRole.RANKING_SIGNAL and score is None:
            return "MissingNumericMetric"
        return None

    def aggregate_score(
        self, metric_name: str, results: list[VerifierResult] | list[BootstrapVerifierResult]
    ) -> float | None:
        ranking = [result for result in results if result.role == VerifierRole.RANKING_SIGNAL]
        sources = ranking or [result for result in results if result.role not in {
            VerifierRole.DIAGNOSTIC_SIGNAL, VerifierRole.ANTI_CHEAT_GATE,
        }]
        scores = [result.score for result in sources if result.score is not None]
        if len(scores) > 1:
            raise ValueError(f"AmbiguousNumericMetric: multiple verifiers emit {metric_name!r}; declare one ranking_signal")
        return scores[0] if scores else None

    def frozen_hash_failures(
        self, frozen: FrozenSpec, workspace: Path
    ) -> dict[str, dict[str, str | None]]:
        failures: dict[str, dict[str, str | None]] = {}
        for rel_path, expected_hash in frozen.verifier_hashes.items():
            path = workspace / rel_path
            actual_hash = (
                sha256_file(path) if path.exists() and path.is_file() else None
            )
            if actual_hash != expected_hash:
                failures[rel_path] = {"expected": expected_hash, "actual": actual_hash}
        return failures

    def git_head(self, workspace: Path) -> str | None:
        try:
            value = git_output(workspace, ["git", "rev-parse", "--verify", "HEAD"])
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None
        return value.strip() or None
