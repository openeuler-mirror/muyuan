"""Durable ThinkThread provider request journal and same-request recovery."""

import hashlib
import time
from pathlib import Path
from typing import Any, Literal
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.infrastructure.clock import utc_timestamp as utc_timestamp
from goal_plus.infrastructure.state.files import exclusive_file_lock, write_json
from goal_plus.ports.state import CandidateRecord, ProviderMutationRecord, RunRecord
from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError, AgentPosixSdkClient
from goal_plus.execution.thinkthread_requests import ThinkThreadRequests

from goal_plus.infrastructure.state.candidate_creation import FileCandidateCreationState


def provider_error(exc: BaseException) -> dict[str, Any]:
    payload = {"type": type(exc).__name__, "message": str(exc)[:2000]}
    if isinstance(exc, AgentPosixBridgeError):
        payload.update(code=exc.code, delivery=exc.delivery, retryable=exc.retryable)
        if exc.rejection is not None:
            payload["rejection"] = exc.rejection
    return {key: value for key, value in payload.items() if value is not None}

THINKTHREAD_MUTATION_METHODS = {
    "baseline_snapshot": "fs.snapshot.create",
    "branch_snapshot": "fs.branch.snapshot",
    "inner_git_settlement": "fs.run",
    "verifier_run": "fs.run",
    "publication_snapshot": "fs.snapshot.patch",
    "apply": "fs.apply",
    "snapshot_remove": "fs.snapshot.remove",
}


class ThinkThreadMutations:
    def __init__(self, root_dir: Path | str) -> None:
        self.state = FileCandidateCreationState(root_dir)

    def _write_run(self, run: RunRecord) -> None:
        write_json(
            self.state.run_dir(run.run_id) / "run.json", run.model_dump(mode="json")
        )

    def _run_transaction(self, run_id: str):
        return exclusive_file_lock(self.state.run_dir(run_id) / "run.lock")

    @staticmethod
    def thinkthread_request_id(
        operation: str,
        run_id: str,
        identity: dict[str, Any],
    ) -> str:
        digest = hashlib.sha256(
            canonical_json(
                {
                    "namespace": "goal-plus.thinkthread.v3",
                    "operation": operation,
                    "run_id": run_id,
                    "identity": identity,
                }
            ).encode("utf-8")
        ).hexdigest()[:32]
        return (
            f"req-{digest[:8]}-{digest[8:12]}-{digest[12:16]}-"
            f"{digest[16:20]}-{digest[20:]}"
        )

    def prepare_provider_mutation(
        self,
        *,
        run_id: str,
        operation: str,
        identity: dict[str, Any],
        params: dict[str, Any],
        candidate_id: str | None = None,
    ) -> ProviderMutationRecord:
        request_id = self.thinkthread_request_id(operation, run_id, identity)
        exact_params = {**params, "requestId": request_id}
        params_sha256 = sha256_text(canonical_json(exact_params))
        with self._run_transaction(run_id):
            run = self.state.read_run(run_id)
            records = run.provider_mutations
            candidate: CandidateRecord | None = None
            if candidate_id is not None:
                candidate = self.state.read_candidate(run_id, candidate_id)
                records = candidate.provider_mutations
            existing = next(
                (item for item in records if item.request_id == request_id),
                None,
            )
            if existing is not None:
                if (
                    existing.operation != operation
                    or existing.identity != identity
                    or existing.params_sha256 != params_sha256
                ):
                    raise RuntimeError(
                        f"ThinkThread RequestId {request_id} was reused with new intent"
                    )
                return existing
            now = utc_timestamp()
            mutation = ProviderMutationRecord(
                request_id=request_id,
                operation=operation,  # type: ignore[arg-type]
                identity=identity,
                params_sha256=params_sha256,
                created_at=now,
                updated_at=now,
            )
            records.append(mutation)
            if candidate is not None:
                self.state.write_candidate(run_id, candidate)
            else:
                self._write_run(run)
            return mutation

    def update_provider_mutation(
        self,
        *,
        run_id: str,
        request_id: str,
        state: str,
        candidate_id: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        close_state: str | None = None,
        close_error: dict[str, Any] | None = None,
        increment_close_attempts: bool = False,
    ) -> ProviderMutationRecord:
        with self._run_transaction(run_id):
            run = self.state.read_run(run_id)
            records = run.provider_mutations
            candidate: CandidateRecord | None = None
            if candidate_id is not None:
                candidate = self.state.read_candidate(run_id, candidate_id)
                records = candidate.provider_mutations
            mutation = next(
                (item for item in records if item.request_id == request_id),
                None,
            )
            if mutation is None:
                raise RuntimeError(
                    f"ThinkThread mutation is not persisted: {request_id}"
                )
            mutation.state = state  # type: ignore[assignment]
            mutation.updated_at = utc_timestamp()
            if result is not None:
                mutation.result = dict(result)
            if error is not None:
                mutation.error = dict(error)
            if close_state is not None:
                mutation.close_state = close_state  # type: ignore[assignment]
                mutation.close_updated_at = utc_timestamp()
            if close_error is not None:
                mutation.close_error = dict(close_error)
            if increment_close_attempts:
                mutation.close_attempts += 1
            if state == "closed":
                mutation.close_state = "closed"
                mutation.close_error = {}
                mutation.close_updated_at = utc_timestamp()
            if candidate is not None:
                self.state.write_candidate(run_id, candidate)
            else:
                self._write_run(run)
            return mutation

    def close_provider_request(
        self,
        *,
        client: AgentPosixSdkClient,
        run_id: str,
        candidate_id: str | None,
        request_id: str,
        method: str,
        operation_state: Literal["succeeded", "failed"] = "succeeded",
    ) -> bool:
        self.update_provider_mutation(
            run_id=run_id,
            candidate_id=candidate_id,
            request_id=request_id,
            state=operation_state,
            close_state="closing",
            close_error={},
            increment_close_attempts=True,
        )
        result = ThinkThreadRequests(client).close(request_id, method)
        closed = result.state == "closed"
        self.update_provider_mutation(
            run_id=run_id,
            candidate_id=candidate_id,
            request_id=request_id,
            state="closed" if closed and operation_state == "succeeded" else operation_state,
            close_state="closed" if closed else "needs_recovery",
            close_error=result.error,
        )
        return closed

    def recover_provider_mutation(
        self,
        *,
        client: AgentPosixSdkClient,
        run_id: str,
        candidate_id: str | None,
        request_id: str,
        method: str,
        deadline: float,
    ) -> dict[str, Any] | None:
        while True:
            try:
                status_timeout = max(
                    0.001,
                    min(30.0, deadline - time.monotonic()),
                )
                observation = ThinkThreadRequests(client).observe(
                    request_id, method,
                    timeout_seconds=status_timeout,
                )
            except AgentPosixBridgeError as exc:
                self.update_provider_mutation(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    request_id=request_id,
                    state="needs_recovery",
                    error={
                        "message": str(exc),
                        "code": exc.code,
                        "delivery": exc.delivery,
                    },
                )
                raise
            if observation.state == "absent":
                return None
            state = observation.request_state
            if observation.state == "pending":
                if time.monotonic() >= deadline:
                    self.update_provider_mutation(
                        run_id=run_id,
                        candidate_id=candidate_id,
                        request_id=request_id,
                        state="needs_recovery",
                        error={"request_state": state},
                    )
                    raise RuntimeError(
                        f"ThinkThreadRequestNeedsRecovery: {request_id} is {state}"
                    )
                time.sleep(0.05)
                continue
            if observation.state in {"succeeded", "cancelled"}:
                return observation.terminal_result(request_id)
            if observation.state == "failed":
                normalized = observation.error
                self.update_provider_mutation(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    request_id=request_id,
                    state="failed",
                    error=normalized,
                )
                self.close_provider_request(
                    client=client,
                    run_id=run_id,
                    candidate_id=candidate_id,
                    request_id=request_id,
                    method=method,
                    operation_state="failed",
                )
                raise AgentPosixBridgeError(
                    f"ThinkThread {method} request failed: {request_id}",
                    error={"rejection": {"error": normalized}},
                )
            self.update_provider_mutation(
                run_id=run_id,
                candidate_id=candidate_id,
                request_id=request_id,
                state="needs_recovery",
                error={"request_state": state},
            )
            raise RuntimeError(
                f"ThinkThreadRequestNeedsRecovery: {request_id} is {state!r}"
            )

    def invoke_provider_mutation(
        self,
        *,
        client: AgentPosixSdkClient,
        run_id: str,
        method: str,
        operation: str,
        identity: dict[str, Any],
        params: dict[str, Any],
        timeout_seconds: float,
        candidate_id: str | None = None,
        status_method: str | None = None,
        recovery_deadline: float | None = None,
    ) -> tuple[dict[str, Any], str]:
        mutation = self.prepare_provider_mutation(
            run_id=run_id,
            candidate_id=candidate_id,
            operation=operation,
            identity=identity,
            params=params,
        )
        request_id = mutation.request_id
        if mutation.state == "closed":
            return dict(mutation.result), request_id
        if mutation.state == "succeeded":
            result = dict(mutation.result)
            self.close_provider_request(
                client=client,
                run_id=run_id,
                candidate_id=candidate_id,
                request_id=request_id,
                method=status_method or method,
            )
            return result, request_id
        exact_params = {**params, "requestId": request_id}
        deadline = time.monotonic() + timeout_seconds + 5.0
        recovering_existing = mutation.state in {
            "platform_mutation_started",
            "needs_recovery",
        }
        if recovery_deadline is not None and recovering_existing:
            deadline = min(deadline, recovery_deadline)
        durable_method = status_method or method
        result: dict[str, Any] | None = None
        if mutation.state in {"platform_mutation_started", "needs_recovery"}:
            result = self.recover_provider_mutation(
                client=client,
                run_id=run_id,
                candidate_id=candidate_id,
                request_id=request_id,
                method=durable_method,
                deadline=deadline,
            )

        if result is None:
            if (
                recovery_deadline is not None
                and recovering_existing
                and time.monotonic() >= deadline
            ):
                self.update_provider_mutation(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    request_id=request_id,
                    state="needs_recovery",
                    error={"recovery_deadline_reached": True},
                )
                raise RuntimeError(
                    f"ThinkThreadRequestNeedsRecovery: {request_id} recovery deadline"
                )
            self.update_provider_mutation(
                run_id=run_id,
                candidate_id=candidate_id,
                request_id=request_id,
                state="platform_mutation_started",
            )
            try:
                invoke_timeout = timeout_seconds
                if recovery_deadline is not None and recovering_existing:
                    invoke_timeout = min(
                        invoke_timeout,
                        max(0.001, recovery_deadline - time.monotonic()),
                    )
                result = client.invoke(
                    method,
                    exact_params,
                    timeout_seconds=invoke_timeout,
                )
            except AgentPosixBridgeError as exc:
                if not exc.completion_unknown and exc.code != "RequestInProgress":
                    recovered = self.recover_provider_mutation(
                        client=client,
                        run_id=run_id,
                        candidate_id=candidate_id,
                        request_id=request_id,
                        method=durable_method,
                        deadline=deadline,
                    )
                    if recovered is None:
                        self.update_provider_mutation(
                            run_id=run_id,
                            candidate_id=candidate_id,
                            request_id=request_id,
                            state="failed",
                            error={
                                "message": str(exc),
                                "code": exc.code,
                                "delivery": exc.delivery,
                            },
                        )
                        raise
                    result = recovered
                else:
                    recovered = self.recover_provider_mutation(
                        client=client,
                        run_id=run_id,
                        candidate_id=candidate_id,
                        request_id=request_id,
                        method=durable_method,
                        deadline=deadline,
                    )
                    if recovered is None:
                        try:
                            result = client.invoke(
                                method,
                                exact_params,
                                timeout_seconds=invoke_timeout,
                            )
                        except AgentPosixBridgeError as replay_error:
                            recovered = self.recover_provider_mutation(
                                client=client,
                                run_id=run_id,
                                candidate_id=candidate_id,
                                request_id=request_id,
                                method=durable_method,
                                deadline=deadline,
                            )
                            if recovered is None:
                                self.update_provider_mutation(
                                    run_id=run_id,
                                    candidate_id=candidate_id,
                                    request_id=request_id,
                                    state="needs_recovery",
                                    error={
                                        "message": str(replay_error),
                                        "delivery": replay_error.delivery,
                                    },
                                )
                                raise RuntimeError(
                                    f"ThinkThreadRequestNeedsRecovery: {request_id}"
                                ) from replay_error
                            result = recovered
                    else:
                        result = recovered
        if result is None:
            raise RuntimeError(f"ThinkThread request returned no result: {request_id}")
        self.update_provider_mutation(
            run_id=run_id,
            candidate_id=candidate_id,
            request_id=request_id,
            state="succeeded",
            result=result,
            error={},
        )
        self.close_provider_request(
            client=client,
            run_id=run_id,
            candidate_id=candidate_id,
            request_id=request_id,
            method=durable_method,
        )
        return result, request_id

    def reconcile_thinkthread_request_closes(
        self,
        run_id: str,
        *,
        client: AgentPosixSdkClient | None = None,
    ) -> dict[str, Any]:
        pending: list[tuple[str | None, ProviderMutationRecord]] = []
        run = self.state.read_run(run_id)
        pending.extend(
            (None, mutation)
            for mutation in run.provider_mutations
            if mutation.state in {"succeeded", "failed"}
            and mutation.close_state != "closed"
        )
        for candidate in self.state.read_candidates(run_id):
            pending.extend(
                (candidate.candidate_id, mutation)
                for mutation in candidate.provider_mutations
                if mutation.state in {"succeeded", "failed"}
                and mutation.close_state != "closed"
            )
        if not pending:
            return {"closed": [], "needs_recovery": []}
        sdk = client or AgentPosixSdkClient()
        try:
            sdk.preflight()
        except (AgentPosixBridgeError, RuntimeError) as exc:
            for candidate_id, mutation in pending:
                self.update_provider_mutation(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    request_id=mutation.request_id,
                    state=mutation.state,
                    close_state="needs_recovery",
                    close_error={
                        "stage": "preflight",
                        "message": str(exc),
                        "code": (
                            exc.code if isinstance(exc, AgentPosixBridgeError) else None
                        ),
                    },
                )
            return {
                "closed": [],
                "needs_recovery": [item.request_id for _, item in pending],
            }
        closed: list[str] = []
        needs_recovery: list[str] = []
        for candidate_id, mutation in pending:
            method = THINKTHREAD_MUTATION_METHODS[mutation.operation]
            if self.close_provider_request(
                client=sdk,
                run_id=run_id,
                candidate_id=candidate_id,
                request_id=mutation.request_id,
                method=method,
                operation_state=(
                    "failed" if mutation.state == "failed" else "succeeded"
                ),
            ):
                closed.append(mutation.request_id)
            else:
                needs_recovery.append(mutation.request_id)
        return {"closed": closed, "needs_recovery": needs_recovery}
