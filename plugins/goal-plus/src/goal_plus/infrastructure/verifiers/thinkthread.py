"""Frozen verifier execution over exact ThinkThread snapshot identities."""

from pathlib import Path
from typing import Any, Literal
from goal_plus.infrastructure.verifiers.output import VERIFIER_LOG_LIMIT_BYTES, bounded_verifier_log
from goal_plus.infrastructure.verifiers.local import (
    LocalVerifiers,
    VERIFIER_PHASE_ENV,
    safe_verifier_name,
)
from goal_plus.domain.artifact_paths import thinkthread_snapshot_path
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import ScoreReport, VerifierResult
from goal_plus.domain.search import FrozenSpec, VerifierCommand, VerifierRole
from goal_plus.domain.serialization import canonical_json
from goal_plus.infrastructure.state.files import write_text
from goal_plus.ports.state import RunRecord
from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError, AgentPosixSdkClient
from goal_plus.thinkthread_artifacts import (
    ThinkThreadSnapshotReader,
    decode_fs_run_output,
)

from goal_plus.infrastructure.artifacts.thinkthread_mutations import (
    ThinkThreadMutations,
)


class ThinkThreadVerifiers:
    def __init__(
        self,
        root_dir: Path | str,
        mutations: ThinkThreadMutations,
        local: LocalVerifiers,
    ) -> None:
        self.root_dir, self.mutations, self.verifiers = Path(root_dir), mutations, local

    def _run_dir(self, run_id: str) -> Path:
        return self.root_dir / "runs" / run_id

    def _candidate_dir(self, run_id: str, candidate_id: str) -> Path:
        return self._run_dir(run_id) / "candidates" / candidate_id

    def thinkthread_hash_failures(
        self,
        *,
        reader: ThinkThreadSnapshotReader,
        artifact_ref: ArtifactRef,
        source_relative_path: str,
        verifier_hashes: dict[str, str],
    ) -> dict[str, dict[str, str | None]]:
        failures: dict[str, dict[str, str | None]] = {}
        for relative_path, expected in verifier_hashes.items():
            snapshot_path = thinkthread_snapshot_path(
                source_relative_path,
                relative_path,
            )
            try:
                actual = reader.file_digest(artifact_ref, snapshot_path)
            except (AgentPosixBridgeError, ValueError):
                actual = None
            if actual != expected:
                failures[relative_path] = {"expected": expected, "actual": actual}
        return failures

    @staticmethod
    def thinkthread_exit_status(
        result: dict[str, Any],
    ) -> tuple[int | None, str | None]:
        exit_status = result.get("exit")
        if not isinstance(exit_status, dict):
            return None, "VerifierInfrastructureFailure"
        kind = exit_status.get("kind")
        if kind == "code" and isinstance(exit_status.get("code"), int):
            code = int(exit_status["code"])
            return code, None if code == 0 else "VerifierCommandFailed"
        if kind == "signal" and isinstance(exit_status.get("signal"), int):
            return -int(exit_status["signal"]), "VerifierSignal"
        return {
            "timeout": (None, "Timeout"),
            "killed": (None, "VerifierKilled"),
            "cancelled": (None, "VerifierCancelled"),
        }.get(str(kind), (None, "VerifierInfrastructureFailure"))

    def run_thinkthread_command(
        self,
        *,
        client: AgentPosixSdkClient,
        run: RunRecord,
        frozen: FrozenSpec,
        candidate_id: str | None,
        artifact_ref: ArtifactRef,
        command: VerifierCommand,
        phase: Literal["origin", "candidate", "promotion"],
        iteration: int,
        command_index: int,
        recovery_deadline: float | None = None,
    ) -> VerifierResult:
        reader = ThinkThreadSnapshotReader(client)
        source_relative_path = str(
            run.source_provider_handle.get("source_relative_path") or "."
        )
        if command.command[0] == "goal-plus-internal":
            known = command.command[1:] == ["check-frozen-hashes"]
            failures = (
                self.thinkthread_hash_failures(
                    reader=reader,
                    artifact_ref=artifact_ref,
                    source_relative_path=source_relative_path,
                    verifier_hashes=frozen.verifier_hashes,
                )
                if known
                else {"command": {"expected": "check-frozen-hashes", "actual": None}}
            )
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=known and not failures,
                score=0.0 if failures else None,
                metrics={"hash_failures": failures},
                failure_class=(
                    None
                    if known and not failures
                    else "FrozenVerifierModified"
                    if known
                    else "UnknownInternalVerifier"
                ),
            )

        cwd = thinkthread_snapshot_path(source_relative_path, command.cwd)
        identity = {
            "candidate_id": candidate_id,
            "snapshot_id": artifact_ref.id,
            "phase": phase,
            "iteration": iteration,
            "command_index": command_index,
            "command_name": command.name,
        }
        params = {
            "snapshotId": artifact_ref.id,
            "writes": "discard",
            "invocation": {
                "argv": list(command.command),
                "cwd": cwd,
                "environment": {
                    VERIFIER_PHASE_ENV: phase,
                    "PYTHONPATH": ".",
                },
            },
            "limits": {
                "timeoutMs": int(command.timeout_seconds * 1000),
                "maxOutputBytes": VERIFIER_LOG_LIMIT_BYTES,
            },
        }
        log_scope = "bootstrap" if phase == "origin" else phase
        log_dir = self._run_dir(run.run_id) / "provider-logs" / log_scope
        if candidate_id is not None:
            log_dir = self._candidate_dir(run.run_id, candidate_id) / "logs" / phase
        log_path = log_dir / (
            f"iteration-{iteration:04d}-{command_index:02d}-"
            f"{safe_verifier_name(command.name)}.log"
        )
        request_id: str | None = None
        try:
            result, request_id = self.mutations.invoke_provider_mutation(
                client=client,
                run_id=run.run_id,
                candidate_id=candidate_id,
                method="fs.run",
                operation="verifier_run",
                identity=identity,
                params=params,
                timeout_seconds=float(command.timeout_seconds) + 5.0,
                recovery_deadline=recovery_deadline,
            )
            stdout, stderr = decode_fs_run_output(result)
            returncode, failure_class = self.thinkthread_exit_status(result)
        except AgentPosixBridgeError as exc:
            if exc.completion_unknown:
                raise
            metrics = {
                "provider": "thinkthread",
                "snapshot_id": artifact_ref.id,
                "error": str(exc),
                "error_code": exc.code,
                "completion_unknown": False,
                "infrastructure_failure": True,
                **({"request_id": request_id} if request_id is not None else {}),
            }
            write_text(log_path, bounded_verifier_log(canonical_json(metrics)))
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics=metrics,
                log_path=log_path,
                failure_class="VerifierInfrastructureFailure",
            )
        except (TypeError, ValueError) as exc:
            metrics = {
                "provider": "thinkthread",
                "snapshot_id": artifact_ref.id,
                "request_id": request_id,
                "error": str(exc),
                "infrastructure_failure": True,
            }
            write_text(log_path, bounded_verifier_log(canonical_json(metrics)))
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics=metrics,
                log_path=log_path,
                failure_class="VerifierInfrastructureFailure",
            )
        write_text(
            log_path,
            bounded_verifier_log(
                canonical_json(
                    {
                        "request_id": request_id,
                        "snapshot_id": artifact_ref.id,
                        "run_key": result.get("runKey"),
                        "exit": result.get("exit"),
                        "output_truncated": result.get("outputTruncated"),
                        "metrics": result.get("metrics"),
                    }
                )
                + "\n--- stdout ---\n"
                + stdout
                + "\n--- stderr ---\n"
                + stderr
            ),
        )
        metrics = self.verifiers.parse_metrics(stdout)
        metrics.update(
            {
                "returncode": returncode,
                "provider": "thinkthread",
                "snapshot_id": artifact_ref.id,
                "request_id": request_id,
                "run_key": result.get("runKey"),
                "output_truncated": bool(result.get("outputTruncated")),
                "provider_metrics": dict(result.get("metrics") or {}),
            }
        )
        passed = returncode == 0 and failure_class is None
        return VerifierResult(
            name=command.name,
            role=command.role,
            passed=passed,
            score=self.verifiers.score_from_metrics(frozen.spec.metric_name, metrics),
            metrics=metrics,
            log_path=log_path,
            failure_class=None if passed else failure_class,
        )

    def run_thinkthread_commands(
        self,
        *,
        client: AgentPosixSdkClient,
        run: RunRecord,
        frozen: FrozenSpec,
        candidate_id: str | None,
        artifact_ref: ArtifactRef,
        commands: list[VerifierCommand],
        phase: Literal["origin", "candidate", "promotion"],
        iteration: int,
        touched_denied_files: bool = False,
        changed_outside_allowed: bool = False,
        recovery_deadline: float | None = None,
    ) -> ScoreReport:
        results: list[VerifierResult] = []
        hash_failures = self.thinkthread_hash_failures(
            reader=ThinkThreadSnapshotReader(client),
            artifact_ref=artifact_ref,
            source_relative_path=str(
                run.source_provider_handle.get("source_relative_path") or "."
            ),
            verifier_hashes=frozen.verifier_hashes,
        )
        if hash_failures:
            results.append(
                VerifierResult(
                    name="frozen_hash_check",
                    role=VerifierRole.ANTI_CHEAT_GATE,
                    passed=False,
                    score=0.0,
                    metrics={"hash_failures": hash_failures},
                    failure_class="FrozenVerifierModified",
                )
            )
        else:
            for command_index, command in enumerate(commands, start=1):
                result = self.run_thinkthread_command(
                    client=client,
                    run=run,
                    frozen=frozen,
                    candidate_id=candidate_id,
                    artifact_ref=artifact_ref,
                    command=command,
                    phase=phase,
                    iteration=iteration,
                    command_index=command_index,
                    recovery_deadline=recovery_deadline,
                )
                results.append(result)
                if result.failure_class == "VerifierInfrastructureFailure":
                    break
        hard_failed = any(
            not item.passed
            and item.role
            in {
                VerifierRole.VALIDITY_GATE,
                VerifierRole.PROCESS_GATE,
                VerifierRole.PROMOTION_GATE,
                VerifierRole.ANTI_CHEAT_GATE,
            }
            for item in results
        )
        process_passed = not hard_failed and all(
            item.passed or item.role == VerifierRole.DIAGNOSTIC_SIGNAL
            for item in results
        )
        score = self.verifiers.aggregate_score(frozen.spec.metric_name, results)
        if not process_passed:
            score = 0.0
        return ScoreReport(
            run_id=run.run_id,
            candidate_id=candidate_id or "origin",
            validity_passed=process_passed,
            process_passed=process_passed,
            promotion_passed=process_passed if phase == "promotion" else None,
            aggregate_score=score,
            verifier_results=results,
            touched_denied_files=touched_denied_files,
            changed_outside_allowed=changed_outside_allowed,
            hardcoding_suspected=bool(hash_failures),
        )
