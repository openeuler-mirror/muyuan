from pathlib import Path
from typing import Any
import os
import json
from goal_plus.infrastructure.state.files import load_json
from goal_plus.shared_dir import SharedDirManager

EXTERNAL_EVIDENCE_DIR_ENV = "GOAL_PLUS_EXTERNAL_EVIDENCE_DIR"
MAX_EXTERNAL_EVIDENCE_BYTES = 256 * 1024


class FileEvidenceAttachments:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir

    def discoverable_tool_ids(self, run_id: str) -> set[str]:
        return SharedDirManager(self.root_dir / "runs" / run_id).discoverable_tool_ids()

    def attach_external_evaluations(
        self,
        run_id: str,
        evidence: list[dict[str, Any]],
    ) -> None:
        directory_value = os.environ.get(EXTERNAL_EVIDENCE_DIR_ENV)
        if not directory_value:
            return
        try:
            paths = sorted(
                path
                for path in Path(directory_value).glob("*.json")
                if not path.name.startswith(".") and path.is_file()
            )
        except OSError:
            return
        for path in paths:
            try:
                if path.stat().st_size > MAX_EXTERNAL_EVIDENCE_BYTES:
                    continue
                payload = load_json(path)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            artifact = payload.get("artifact")
            evaluation = payload.get("evaluation")
            if (
                not isinstance(artifact, dict)
                or artifact.get("source") != "goal_plus_best"
                or artifact.get("run_id") != run_id
                or not isinstance(evaluation, dict)
            ):
                continue
            for entry in evidence:
                if (
                    entry.get("candidate_id") == artifact.get("candidate_id")
                    and entry.get("iteration") == artifact.get("iteration")
                    and (entry.get("commit") or entry.get("git_head"))
                    == artifact.get("commit")
                ):
                    external = dict(evaluation)
                    source = payload.get("source")
                    external["source"] = (
                        source if isinstance(source, str) else "external"
                    )
                    entry.setdefault("external_evaluations", []).append(external)
                    break
        for entry in evidence:
            evaluations = entry.get("external_evaluations")
            if isinstance(evaluations, list):
                evaluations.sort(
                    key=lambda item: (
                        str(item.get("published_at") or ""),
                        str(item.get("round_id") or ""),
                    )
                )
