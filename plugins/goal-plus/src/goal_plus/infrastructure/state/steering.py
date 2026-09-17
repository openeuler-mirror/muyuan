"""Spent steering opportunities only; no queue, scan or recovery mechanism."""

from typing import Literal
import re

from goal_plus.domain.adaptive.models import SteeringAttempt
from goal_plus.domain.adaptive.search_diagnostic_guidance import SearchDiagnosticInput, DiagnosticEvaluation
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json
from goal_plus.ports.steering import SteeringDiagnosticObservation


class FileSteeringState(FileCandidateQueries):
    def lock_run(self, run_id: str):
        return exclusive_file_lock(self.run_dir(run_id) / "run.lock")

    def _path(self, run_id: str, attempt_key: str):
        # These are generated identities, not paths supplied by a candidate.
        if re.fullmatch(r"steering_[0-9a-f]{24}", attempt_key) is None:
            raise ValueError("invalid steering attempt key")
        return self.run_dir(run_id) / "steering-attempts" / f"{attempt_key}.json"

    def read_attempt(self, run_id: str, attempt_key: str) -> SteeringAttempt | None:
        path = self._path(run_id, attempt_key)
        if not path.exists():
            return None
        attempt = SteeringAttempt.model_validate(load_json(path))
        if attempt.source.run_id != run_id or attempt.attempt_key != attempt_key:
            raise ValueError("steering attempt identity mismatch")
        return attempt

    def consume_attempt(self, attempt: SteeringAttempt) -> bool:
        """Caller owns run.lock across admission, read and atomic file replacement."""
        if self.read_attempt(attempt.source.run_id, attempt.attempt_key) is not None:
            return False
        write_json(self._path(attempt.source.run_id, attempt.attempt_key), attempt.model_dump(mode="json"))
        return True

    def list_attempts(self, run_id: str, candidate_id: str) -> tuple[SteeringAttempt, ...]:
        attempts = [self.read_attempt(run_id, path.stem)
                    for path in sorted((self.run_dir(run_id) / "steering-attempts").glob("*.json"))]
        return tuple(item for item in attempts if item is not None and item.source.candidate_id == candidate_id)

    def _diagnostic_path(self, run_id, key):
        self._path(run_id, key)  # Validate the same generated identity, not a caller-supplied path.
        return self.run_dir(run_id) / "steering-diagnostics" / f"{key}.json"

    def read_diagnostic(self, run_id, key):
        path = self._diagnostic_path(run_id, key)
        if not path.exists():
            return None
        data = load_json(path)
        input = SearchDiagnosticInput.model_validate(data["input"])
        if (data["run_id"] != run_id or data["key"] != key or data["input_hash"] != input.input_hash
                or data["result_hash"] != sha256_text(canonical_json(data["result"]))):
            raise ValueError("diagnostic input identity mismatch")
        return input, DiagnosticEvaluation.model_validate(data["result"])

    def write_diagnostic(self, run_id, key, input, result, *, mode):
        path = self._diagnostic_path(run_id, key)
        if path.exists():
            raise ValueError("diagnostic input is immutable")
        write_json(path, {"run_id": run_id, "key": key, "mode": mode,
            "input": input.model_dump(mode="json"), "input_hash": input.input_hash,
            "result_hash": sha256_text(canonical_json(result.model_dump(mode="json"))),
            "result": result.model_dump(mode="json")})

    def observe_attempt(self, run_id: str, attempt_key: str,
                        observation: Literal["returned", "unknown", "not_sent"]) -> None:
        with self.lock_run(run_id):
            attempt = self.read_attempt(run_id, attempt_key)
            if attempt is None:
                raise ValueError("cannot observe an unconsumed steering opportunity")
            attempt.observation = observation
            write_json(self._path(run_id, attempt_key), attempt.model_dump(mode="json"))

    def read_diagnostic_observation(self, run_id: str, key: str) -> SteeringDiagnosticObservation | None:
        if self.read_diagnostic(run_id, key) is None:
            return None
        data = load_json(self._diagnostic_path(run_id, key))
        if "observation" not in data:
            return None
        if data["observation_hash"] != sha256_text(canonical_json(data["observation"])):
            raise ValueError("diagnostic observation integrity mismatch")
        observation = SteeringDiagnosticObservation.model_validate(data["observation"])
        if observation.result.attempt_key not in {None, key}:
            raise ValueError("diagnostic observation identity mismatch")
        return observation

    def observe_diagnostic(self, run_id: str, key: str, observation: SteeringDiagnosticObservation) -> None:
        if observation.result.attempt_key not in {None, key}:
            raise ValueError("diagnostic observation identity mismatch")
        with self.lock_run(run_id):
            if self.read_diagnostic(run_id, key) is None:
                raise ValueError("cannot observe a missing diagnostic")
            if self.read_diagnostic_observation(run_id, key) is not None:
                return  # Preserve the first observation; never promote it to a delivery queue.
            path = self._diagnostic_path(run_id, key)
            data = load_json(path)
            data["observation"] = observation.model_dump(mode="json")
            data["observation_hash"] = sha256_text(canonical_json(data["observation"]))
            write_json(path, data)
