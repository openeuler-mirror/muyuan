"""Frozen verifier copies and FrozenSpec persistence."""

import shutil
from pathlib import Path

from goal_plus.domain.search import (
    CandidateOrigin,
    DispatchGroupState,
    FrozenSpec,
    validate_dispatch_state_payload,
)
from goal_plus.infrastructure.state.files import (
    exclusive_file_lock,
    load_json,
    sha256_file,
    write_json,
)


class FileFrozenSpecState:
    def __init__(self, root_dir: Path | str) -> None:
        self.specs_dir = Path(root_dir) / "specs"
        self.runs_dir = Path(root_dir) / "runs"

    def read_candidate_origin(self, dispatch_group_id: str, dispatch_target_index: int) -> CandidateOrigin:
        matches = []
        for path in self.runs_dir.glob("*/run.json"):
            payload = load_json(path)
            validate_dispatch_state_payload(payload)
            raw = payload.get("dispatch_group")
            if raw is not None and raw.get("dispatch_group_id") == dispatch_group_id:
                fork = DispatchGroupState.model_validate(raw)
                matches.extend(b.binding for b in fork.targets if b.dispatch_target_index == dispatch_target_index)
        if len(matches) != 1:
            raise ValueError("FrozenSpecLineageMismatch: fork branch does not exist or is ambiguous")
        return matches[0]

    def copy_verifiers(
        self, frozen_spec_id: str, entries: list[tuple[Path, str]]
    ) -> dict[str, str]:
        paths = {}
        for artifact, relative_path in entries:
            destination = (
                self.specs_dir / frozen_spec_id / "frozen_verifiers" / relative_path
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with exclusive_file_lock(self.specs_dir / frozen_spec_id / "freeze.lock"):
                if destination.exists():
                    if sha256_file(destination) != sha256_file(artifact):
                        raise ValueError("FrozenSpecConflict: frozen verifier cannot be overwritten")
                else:
                    shutil.copy2(artifact, destination)
            paths[relative_path] = str(destination)
        return paths

    def write(self, frozen: FrozenSpec) -> None:
        path = self.specs_dir / frozen.frozen_spec_id / "frozen_spec.json"
        with exclusive_file_lock(path.parent / "freeze.lock"):
            if path.exists():
                existing = self.read(frozen.frozen_spec_id)
                if (existing.spec_hash != frozen.spec_hash or existing.spec != frozen.spec
                        or existing.verifier_hashes != frozen.verifier_hashes
                        or existing.source_artifact_ref != frozen.source_artifact_ref
                        or (existing.parent_frozen_spec_id, existing.revision_reason, existing.dispatch_group_id, existing.dispatch_target_index)
                        != (frozen.parent_frozen_spec_id, frozen.revision_reason, frozen.dispatch_group_id, frozen.dispatch_target_index)):
                    raise ValueError("FrozenSpecConflict: an existing spec cannot be overwritten")
                return
            write_json(path, frozen.model_dump(mode="json"))

    def read(self, frozen_spec_id: str) -> FrozenSpec:
        return FrozenSpec.model_validate(
            load_json(self.specs_dir / frozen_spec_id / "frozen_spec.json")
        )
