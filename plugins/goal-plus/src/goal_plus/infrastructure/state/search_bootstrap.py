"""Frozen verifier copies and FrozenSpec persistence."""

import shutil
from pathlib import Path

from goal_plus.domain.search import FrozenSpec
from goal_plus.infrastructure.state.files import write_json


class FileFrozenSpecState:
    def __init__(self, root_dir: Path | str) -> None:
        self.specs_dir = Path(root_dir) / "specs"

    def copy_verifiers(
        self, frozen_spec_id: str, entries: list[tuple[Path, str]]
    ) -> dict[str, str]:
        paths = {}
        for artifact, relative_path in entries:
            destination = (
                self.specs_dir / frozen_spec_id / "frozen_verifiers" / relative_path
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(artifact, destination)
            paths[relative_path] = str(destination)
        return paths

    def write(self, frozen: FrozenSpec) -> None:
        write_json(
            self.specs_dir / frozen.frozen_spec_id / "frozen_spec.json",
            frozen.model_dump(mode="json"),
        )
