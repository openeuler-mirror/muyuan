"""Read-only report and monitor views for verifier-first Search."""

from pathlib import Path

from goal_plus.infrastructure.state.allocation import load_allocation_decisions
from goal_plus.infrastructure.state.verifier import FileVerifierState
from goal_plus.ports.report import SearchVerifierViews


def load_search_verifier_views(run_dir: Path | str) -> SearchVerifierViews:
    directory = Path(run_dir).resolve()
    run_id = directory.name
    state = FileVerifierState(directory.parents[1])
    return SearchVerifierViews(
        verifier_tasks=tuple(state.load_verifier_tasks(run_id)),
        verifier_records=tuple(state.load_verifier_records(run_id)),
        allocation_decisions=tuple(load_allocation_decisions(directory)),
    )


__all__ = ["load_search_verifier_views"]
