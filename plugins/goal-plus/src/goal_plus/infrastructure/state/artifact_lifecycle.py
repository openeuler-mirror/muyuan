"""Snapshot reference and host cleanup facts in the existing file layout."""

import json
from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.infrastructure.state.files import load_json, exclusive_file_lock
from goal_plus.ports.artifact_lifecycle import ExternalSnapshotReferences


class FileArtifactLifecycleState(FileCandidateQueries):
    def lock_run(self, run_id: str):
        return exclusive_file_lock(self.run_dir(run_id) / "run.lock")

    def external_references(self, run_id: str) -> list[ExternalSnapshotReferences]:
        references = []
        for run_path in sorted((self.root_dir / "runs").glob("*/run.json")):
            if run_path.parent.name == run_id:
                continue
            try:
                payload = load_json(run_path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            candidates = []
            for path in sorted(
                (run_path.parent / "candidates").glob("*/candidate.json")
            ):
                try:
                    candidates.append(load_json(path))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            references.append(
                ExternalSnapshotReferences(run_path.parent.name, payload, candidates)
            )
        return references

    def reports_durable(self, run_id: str) -> bool:
        path = self.run_dir(run_id) / "report.md"
        return path.is_file() and path.with_suffix(".html").is_file()

    def session_resource_states(self, run_id: str) -> list[tuple[str, str]]:
        blockers: list[tuple[str, str]] = []
        for path in sorted((self.root_dir / "host-scopes").glob("*/thinkthread-sessions/*.json")):
            resource = load_json(path)
            if resource.get("run_id") == run_id:
                blockers.append((path.stem, str(resource.get("cleanup_state") or "open")))
        return blockers

    def annotation_state(self, run_id: str) -> str | None:
        path = (
            self.root_dir
            / "host-pools"
            / "thinkthread-annotations"
            / run_id
            / "state.json"
        )
        if not path.is_file():
            return None
        try:
            state = str(load_json(path).get("status") or "unknown")
        except (OSError, ValueError, json.JSONDecodeError):
            return "unreadable"
        return state
