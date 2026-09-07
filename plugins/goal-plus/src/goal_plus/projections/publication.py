"""Present the durable provider publication without repairing historical state."""

from typing import Any
from goal_plus.ports.state import RunRecord


def publication_summary(run: RunRecord) -> dict[str, Any]:
    outcome = run.publication_outcome
    promotion = run.promotion
    payload = outcome.model_dump(mode="json") if outcome is not None else None
    if payload is None and promotion is not None:
        payload = {
            "state": "failed" if promotion.state == "apply_failed" else promotion.state,
            "publication_artifact_ref": (
                promotion.publication_artifact_ref.model_dump(mode="json")
                if promotion.publication_artifact_ref is not None
                else None
            ),
            "selected_artifact_ref": promotion.selected_artifact_ref.model_dump(
                mode="json"
            ),
            "artifact_path": str(promotion.artifact_path)
            if promotion.artifact_path
            else None,
            "apply_request_id": promotion.apply_request_id,
            "applied_source_revision": promotion.applied_source_revision,
            "error": promotion.error,
        }
    state = payload["state"] if payload else "not_applicable"
    result = run.search_result
    if result == "unknown" and run.state == "promoted" and promotion is not None:
        result = "passed"
    return {
        "search_result": result,
        "publication_result": "pending_integration"
        if state == "awaiting_main_integration"
        else state,
        "publication_outcome": payload,
    }
